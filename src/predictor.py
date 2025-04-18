# -*- coding:UTF-8 -*-
import re
from transformers import AutoTokenizer
from utils.mismatched_utils import *
from src.dataset import Seq2EditVocab, MyCollate
from utils.helpers import INCORRECT_LABEL, KEEP_LABEL, PAD_LABEL, START_TOKEN, UNK_LABEL, get_target_sent_by_edits
from src.model import GECToRModel
from random import seed
import deepspeed
import os
import torch
from deepspeed.runtime.fp16.loss_scaler import DynamicLossScaler
import torch.serialization

class Predictor:
    def __init__(self, args):
        self.fix_seed()
        deepspeed.init_distributed()
        self.device = args.device if args.device else (
            "cuda" if torch.cuda.is_available() else "cpu")
        self.iteration_count = args.iteration_count
        self.min_seq_len = args.min_seq_len
        self.max_num_tokens = args.max_num_tokens
        self.min_error_probability = args.min_error_probability
        self.max_pieces_per_token = args.max_pieces_per_token
        self.debug_force_edit = getattr(args, 'debug_force_edit', 0)
        self.vocab = Seq2EditVocab(
            args.detect_vocab_path, args.correct_vocab_path, unk2keep=bool(args.unk2keep))
        
        # DEBUG: Print vocabs
        print(f"DEBUG: Loaded detect vocab from {args.detect_vocab_path}")
        print(f"DEBUG: Loaded correct vocab from {args.correct_vocab_path}")
        print(f"DEBUG: KEEP_LABEL id: {self.vocab.correct_vocab['tag2id'][KEEP_LABEL]}")
        print(f"DEBUG: Using model from: {args.pretrained_transformer_path}")
        print(f"DEBUG: Using checkpoint from: {args.ckpt_path}")
        
        # Print some sample label IDs to verify vocab is loaded correctly
        print("DEBUG: Sample labels from correction vocabulary:")
        tag2id_dict = self.vocab.correct_vocab["tag2id"].tag2id  # Access the underlying dictionary
        for i, (tag, tag_id) in enumerate(tag2id_dict.items()):
            if i >= 20:  # Show first 20
                break
            print(f"  '{tag}': {tag_id}")
        
        # Check if there are any non-KEEP/PAD/UNK labels
        special_labels = {KEEP_LABEL, PAD_LABEL, UNK_LABEL}
        non_special_labels = [tag for tag in tag2id_dict.keys() 
                             if tag not in special_labels]
        print(f"DEBUG: Found {len(non_special_labels)} non-special labels (first 10):")
        for label in non_special_labels[:10]:
            print(f"  '{label}'")
            
        if not non_special_labels:
            print("DEBUG: ERROR - No correction labels found in vocabulary!")
            print("DEBUG: This explains why no corrections are being made.")
        self.base_tokenizer = AutoTokenizer.from_pretrained(
            args.pretrained_transformer_path, do_basic_tokenize=False)
        self.base_tokenizer_vocab = self.base_tokenizer.get_vocab()
        if bool(args.special_tokens_fix):  # for roberta
            self.base_tokenizer.add_tokens([START_TOKEN], special_tokens=True)
            # set start_token to unk_token_id is no longer supported via transformers tokenizer
            # since access the vocab is implemented by calling get_vocab() which create a new instance,
            # in this case, we cannot actually change the vocab.
            # Instead, we can get the vocab and change it, then use it directly later on.
            # self.base_tokenizer.vocab[START_TOKEN] = self.base_tokenizer.unk_token_id
            self.base_tokenizer_vocab[START_TOKEN] = self.base_tokenizer.unk_token_id
        self.mismatched_tokenizer = MisMatchedTokenizer(
            self.base_tokenizer, self.base_tokenizer_vocab, self.max_pieces_per_token)
        self.collate_fn = MyCollate(
            max_len=self.max_num_tokens,
            input_pad_id=self.base_tokenizer.pad_token_id,
            detect_pad_id=self.vocab.detect_vocab["tag2id"][PAD_LABEL],
            correct_pad_id=self.vocab.correct_vocab["tag2id"][PAD_LABEL])
        self.model = self.init_model(args)
        self.model.eval()

    def init_model(self, args):
        model = GECToRModel(
            encoder_path=args.pretrained_transformer_path,
            num_detect_tags=len(self.vocab.detect_vocab["id2tag"]),
            num_correct_tags=len(self.vocab.correct_vocab["id2tag"]),
            additional_confidence=args.additional_confidence,
            dp_rate=0.0,
            detect_pad_id=self.vocab.detect_vocab["tag2id"][PAD_LABEL],
            correct_pad_id=self.vocab.correct_vocab["tag2id"][PAD_LABEL],
            detect_incorrect_id=self.vocab.detect_vocab["tag2id"][INCORRECT_LABEL],
            correct_keep_id=self.vocab.correct_vocab["tag2id"][KEEP_LABEL],
            sub_token_mode=args.sub_token_mode,
            device=self.device
        )
        ds_engine, _, _, _ = deepspeed.initialize(
            args=args, model=model, model_parameters=model.parameters())
        
        # Add DynamicLossScaler to the safe globals for PyTorch 2.6 compatibility
        try:
            torch.serialization.add_safe_globals([DynamicLossScaler])
        except AttributeError:
            # Older PyTorch versions don't have add_safe_globals
            pass
        
        load_dir, tag = os.path.split(args.ckpt_path)
        print(f"DEBUG: Loading checkpoint from directory: {load_dir}, tag: {tag}")
        try:
            print("DEBUG: Attempting to load checkpoint with default settings")
            ds_engine.load_checkpoint(load_dir=load_dir, tag=tag, load_module_only=True, load_optimizer_states=False, load_lr_scheduler_states=False)
            print("DEBUG: Successfully loaded checkpoint")
        except Exception as e:
            print(f"DEBUG: Error loading checkpoint: {str(e)}")
            if "weights_only" in str(e):
                # For PyTorch 2.6+, try loading with weights_only=False
                print("Warning: Attempting to load checkpoint with weights_only=False due to PyTorch 2.6+ compatibility issues")
                # We need to patch deepspeed's checkpoint loading to use weights_only=False
                # This is a bit hacky but should work for compatibility
                import types
                from functools import partial
                
                original_torch_load = torch.load
                # Monkey patch torch.load to use weights_only=False
                torch.load = lambda *args, **kwargs: original_torch_load(*args, **kwargs, weights_only=False)
                
                try:
                    ds_engine.load_checkpoint(load_dir=load_dir, tag=tag, load_module_only=True, load_optimizer_states=False, load_lr_scheduler_states=False)
                    print("DEBUG: Successfully loaded checkpoint with weights_only=False")
                finally:
                    # Restore original torch.load
                    torch.load = original_torch_load
            else:
                raise

        return ds_engine

    def handle_batch(self, full_batch):
        final_batch = full_batch[:]
        # {sent idx: sent}, used for stop iter early
        prev_preds_dict = {idx: [sent] for idx, sent in enumerate(final_batch)}
        short_skip_id_set = set([idx for idx, sent in enumerate(
            final_batch) if len(sent) < self.min_seq_len])
        # idxs for len(sent) > min_seq_len
        pred_ids = [idx for idx in range(
            len(full_batch)) if idx not in short_skip_id_set]
        total_updates = 0

        for n_iter in range(self.iteration_count):
            ori_batch = [final_batch[i] for i in pred_ids]
            batch_input_dict, truncated_seq_lengths = self.preprocess(ori_batch)
            if not batch_input_dict:
                break
            label_probs, label_ids, max_detect_incor_probs = self.predict(
                batch_input_dict)
            del batch_input_dict
            # list of sents(each sent is a list of target tokens)
            pred_batch = self.postprocess(
                ori_batch, truncated_seq_lengths, label_probs, label_ids, max_detect_incor_probs)

            final_batch, pred_ids, cnt = \
                self.update_final_batch(final_batch, pred_ids, pred_batch,
                                        prev_preds_dict)
            total_updates += cnt
            if not pred_ids:
                break
        return final_batch, total_updates

    def predict(self, batch_inputs):
        with torch.no_grad():
            for k, v in batch_inputs.items():
                batch_inputs[k] = v.cuda()
            outputs = self.model(batch_inputs)
        label_probs, label_ids = torch.max(
            outputs['class_probabilities_labels'], dim=-1)
        max_detect_incor_probs = outputs['max_error_probability']
        return label_probs.tolist(), label_ids.tolist(), max_detect_incor_probs.tolist()

    def preprocess(self, seqs):
        seq_lens = [len(seq) for seq in seqs if seq]
        if not seq_lens:
            return []
        input_dict_batch = []
        truncated_seq_lengths = []
        for words in seqs:
            words = [START_TOKEN] + words
            input_ids, offsets, truncated_seq_length = self.mismatched_tokenizer.encode(words, add_special_tokens=False, max_tokens=self.max_num_tokens)
            words = words[:truncated_seq_length]
            truncated_seq_lengths.append(truncated_seq_length)
            input_dict = self.build_input_dict(input_ids, offsets, len(words))
            input_dict_batch.append(input_dict)
        batch_input_dict = self.collate_fn(input_dict_batch)
        for k, v in batch_input_dict.items():
            batch_input_dict[k] = v.to(self.device)
        return batch_input_dict, truncated_seq_lengths

    def postprocess(self, batch, truncated_seq_lengths, batch_label_probs, batch_label_ids, batch_incor_probs):
        keep_id = self.vocab.correct_vocab["tag2id"][KEEP_LABEL]
        all_results = []
        for tokens, truncated_seq_length, label_probs, label_ids, incor_prob in zip(batch, truncated_seq_lengths, batch_label_probs,
                                                              batch_label_ids, batch_incor_probs):
            # since we add special tokens before truncation, max_len should minus 1. This is different from original gector.
            edits = []

            # DEBUG: Print prediction details
            sent = " ".join(tokens)
            if len(sent) > 50:
                sent = sent[:50] + "..."
            print(f"DEBUG: Processing: {sent}")
            print(f"DEBUG: max_label_id={max(label_ids)}, keep_id={keep_id}, is_all_keep={max(label_ids) == keep_id}")
            print(f"DEBUG: max_incor_prob={incor_prob}, min_threshold={self.min_error_probability}")
            
            # Print all labels and their IDs for analysis
            id_to_tag = self.vocab.correct_vocab["id2tag"]
            print(f"DEBUG: Label ID mapping:")
            for i, label_id in enumerate(label_ids):
                if i < len(tokens):
                    token = tokens[i] if i > 0 else START_TOKEN
                    label = id_to_tag[label_id]
                    print(f"  Token: '{token}', Label ID: {label_id}, Label: '{label}'")
            
            # Force an edit to verify edit application works if debug flag is set
            if bool(self.debug_force_edit) and len(tokens) > 3:
                print("DEBUG: Forcing test edits to verify edit application works")
                # Add a replacement edit for every 3rd token
                for i in range(2, min(len(tokens), 10), 3):  # Limit to first 10 tokens
                    forced_edit = (i, i+1, f"FORCED_EDIT_{i}", 1.0)
                    edits.append(forced_edit)
                    print(f"DEBUG: Added forced edit: {forced_edit} for token '{tokens[i]}'")
            
            # skip the whole sent if all labels are $KEEP and not in debug mode
            if max(label_ids) == keep_id and not bool(self.debug_force_edit):
                print("DEBUG: Skipping - all labels are KEEP")
                all_results.append(tokens)
                continue

            # if max detect_incor_probs < min_error_prob, skip
            if incor_prob < self.min_error_probability:
                print("DEBUG: Skipping - max error probability below threshold")
                all_results.append(tokens)
                continue

            for idx in range(truncated_seq_length):
                if idx == 0:
                    token = START_TOKEN
                else:
                    # tokens in ori_batch don't have "$START" token, thus offset = 1
                    token = tokens[idx-1]
                if label_ids[idx] == keep_id:
                    continue
                # prediction for \s matched token is $keep, for spellcheck task.
                if re.search("\s+", token):
                    continue
                label = self.vocab.correct_vocab["id2tag"][label_ids[idx]]
                # Debug label detection
                print(f"DEBUG: Found non-$KEEP label at pos {idx}: '{label}' for token '{token}' with prob {label_probs[idx]}")
                
                action = self.get_label_action(
                    token, idx, label_probs[idx], label)

                if not action:
                    print(f"DEBUG: Action discarded - label '{label}' prob {label_probs[idx]} < {self.min_error_probability} or special label")
                    continue
                
                print(f"DEBUG: Adding edit: {action} for token '{token}'")
                edits.append(action)
            # Debug final edits
            print(f"DEBUG: Final edits for sentence: {edits}")
            if edits:
                print(f"DEBUG: Original text: '{' '.join(tokens)}'")
                result = get_target_sent_by_edits(tokens, edits)
                print(f"DEBUG: Edited text: '{' '.join(result)}'")
                all_results.append(result)
            else:
                print("DEBUG: No edits applied, keeping original")
                all_results.append(tokens)
        return all_results

    def update_final_batch(self, final_batch, pred_ids, pred_batch,
                           prev_preds_dict):

        new_pred_ids = []
        total_updated = 0

        for i, ori_id in enumerate(pred_ids):
            ori_tokens = final_batch[ori_id]
            pred_tokens = pred_batch[i]
            prev_preds = prev_preds_dict[ori_id]

            if ori_tokens != pred_tokens:
                if pred_tokens not in prev_preds:
                    final_batch[ori_id] = pred_tokens
                    new_pred_ids.append(ori_id)
                    prev_preds_dict[ori_id].append(pred_tokens)
                else:
                    final_batch[ori_id] = pred_tokens
                total_updated += 1
            else:
                continue
        return final_batch, new_pred_ids, total_updated

    def get_label_action(self, token: str, idx: int, label_prob: float, label: str):
        # Debug label action generation
        print(f"DEBUG: get_label_action for token='{token}', label='{label}', prob={label_prob}")
        
        if label_prob < self.min_error_probability:
            print(f"DEBUG: Rejecting - probability {label_prob} < threshold {self.min_error_probability}")
            return None
        
        if label in [UNK_LABEL, PAD_LABEL, KEEP_LABEL]:
            print(f"DEBUG: Rejecting - special label {label}")
            return None

        if label.startswith("$REPLACE_") or label.startswith("$TRANSFORM_") or label == "$DELETE":
            start_pos = idx
            end_pos = idx + 1
        elif label.startswith("$APPEND_") or label.startswith("$MERGE_"):
            start_pos = idx + 1
            end_pos = idx + 1
        else:
            print(f"DEBUG: Unknown label format: {label}")

        if label == "$DELETE":
            processed_label = ""
        elif label.startswith("$TRANSFORM_") or label.startswith("$MERGE_"):
            processed_label = label[:]
        else:
            processed_label = label[label.index("_")+1:]
            
        action = (start_pos - 1, end_pos - 1, processed_label, label_prob)
        print(f"DEBUG: Created action: {action}")
        return action

    def build_input_dict(self, input_ids, offsets, word_level_len):
        token_type_ids = [0 for _ in range(len(input_ids))]
        attn_mask = [1 for _ in range(len(input_ids))]
        word_mask = [1 for _ in range(word_level_len)]
        input_dict = {
            "input_ids": input_ids,
            "token_type_ids": token_type_ids,
            "attention_mask": attn_mask,
            "offsets": offsets,
            "word_mask": word_mask}
        return input_dict

    def fix_seed(self):
        torch.manual_seed(1)
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = True
        seed(43)
