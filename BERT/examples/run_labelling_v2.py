# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Run BERT on SQuAD."""

from __future__ import absolute_import, division, print_function

import argparse
import collections
import json
import logging
import math
import os
import random
import sys
from io import open
from datetime import datetime

import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
from sklearn.metrics import f1_score, precision_score, recall_score

from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from pytorch_pretrained_bert.modeling import BertForQuestionAnswering, BertConfig, WEIGHTS_NAME, CONFIG_NAME
from pytorch_pretrained_bert.modeling_label import BertForLabelling
from pytorch_pretrained_bert.optimization import BertAdam, warmup_linear
from pytorch_pretrained_bert.tokenization import (BasicTokenizer,
                                                  BertTokenizer,
                                                  whitespace_tokenize)

if sys.version_info[0] == 2:
    import cPickle as pickle
else:
    import pickle

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)


class LabellingExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self,
                 qas_id,
                 doc_tokens,
                 insertion,
                 label_seq=None):
        self.qas_id = qas_id
        self.doc_tokens = doc_tokens
        self.insertion = insertion
        self.label_seq = label_seq  # int list

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        s = ""
        s += "qas_id: %s" % self.qas_id
        s += ", doc_tokens: [%s]" % (" ".join(self.doc_tokens))
        s += ", insertion: [%s]" % (" ".join(self.insertion))
        if self.label_seq:
            s += ", label_seq: %d" % (self.label_seq)
        return s


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self,
                 unique_id,
                 example_index,
                 tokens,
                 token_to_orig_map,
                 input_ids,
                 input_mask,
                 segment_ids,
                 label_seq=None):
        self.unique_id = unique_id
        self.example_index = example_index
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_seq = label_seq


def read_label_examples(input_file, is_training):
    examples = []
    with open(input_file, "r", encoding='utf-8') as reader:
        for qas_id, line in enumerate(reader):
            sentences = line.split('\t')
            if (len(sentences) < 2) or (not sentences[0].strip()) or (not sentences[1].strip()):
                continue
            doc_tokens = sentences[0].split(' ')
            insertion = sentences[1].strip()
            if is_training:
                if (len(sentences) < 3) or (not sentences[2].strip()):
                    continue
                label_seq = [int(item) for item in sentences[2].strip().split(' ')]
                if len(doc_tokens) == len(label_seq):
                    label_seq.append(0)
                assert len(doc_tokens) + 1 == len(label_seq)
                example = LabellingExample(
                    qas_id=qas_id,
                    doc_tokens=doc_tokens,
                    insertion=insertion,
                    label_seq=label_seq)
                examples.append(example)
            else:
                example = LabellingExample(
                    qas_id=qas_id,
                    doc_tokens=doc_tokens,
                    insertion=insertion,
                    label_seq=None)
                examples.append(example)
            # if qas_id > 100000:
            #     break
    return examples


def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 doc_stride, max_insertion_length, is_training):
    """Loads a data file into a list of `InputBatch`s."""

    unique_id = 1000000000

    features = []
    for (example_index, example) in enumerate(examples):
        insertion_sub_tokens = tokenizer.tokenize(example.insertion)

        if len(insertion_sub_tokens) > max_insertion_length:
            insertion_sub_tokens = insertion_sub_tokens[0:max_insertion_length]

        # index for each original token
        tok_to_orig_index = []
        # index for each sub-token
        orig_to_tok_index = []
        # sub-tokens, rather than the original tokens
        all_doc_tokens = []
        for (i, token) in enumerate(example.doc_tokens):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)

        # The -3 -n accounts for [CLS], [SEP]*2, insertion
        max_tokens_for_doc = max_seq_length - 3 - len(insertion_sub_tokens)

        # sub-tokens with tags, such as the "all_doc_tokens" + "CLS"
        tokens = []
        token_to_orig_map = {}
        token_is_max_context = {}
        segment_ids = []
        # input_mask = []
        # labeling tokens for whether it is the insert position
        label_seq = []
        tokens.append("[CLS]")
        segment_ids.append(0)
        label_seq.append(0)
        # input_mask.append(0)

        is_ins_pos_found = False
        for i in range(min(len(all_doc_tokens), max_tokens_for_doc)):
            # sub-token index for the sub-token
            split_token_index = i
            # original token index for the sub-token
            orig_index = tok_to_orig_index[split_token_index]
            # record of the original index, according to the sequence with tags
            token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

            if is_training and (not is_ins_pos_found) and (example.label_seq[orig_index] == 1):
                label_seq.append(1)
                # keep only one insertion position, rather than many positions of sub-tokens
                is_ins_pos_found = True
            else:
                label_seq.append(0)

            tokens.append(all_doc_tokens[split_token_index])
            segment_ids.append(0)
            # input_mask.append(1)

        tokens.append("[SEP]")
        segment_ids.append(0)
        if is_training and (not is_ins_pos_found) and (example.label_seq[-1] == 1):
            label_seq.append(1)
            is_ins_pos_found = True
        else:
            label_seq.append(0)
        # input_mask.append(1)

        # add token for the last position
        # tok_to_orig_index[(min(len(all_doc_tokens), max_tokens_for_doc)) - 1] indicates the position of the last token
        # ^ +1 means the insertion position is at the last
        token_to_orig_map[len(tokens)] = tok_to_orig_index[(min(len(all_doc_tokens), max_tokens_for_doc)) - 1] + 1

        if is_training and (not is_ins_pos_found):
            continue

        # add insertion in the sequence
        for insertion_sub_token in insertion_sub_tokens:
            tokens.append(insertion_sub_token)
            segment_ids.append(1)
            label_seq.append(0)
            # input_mask.append(0)

        tokens.append("[SEP]")
        segment_ids.append(1)
        label_seq.append(0)
        # input_mask.append(0)
        # add finished


        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        input_mask = [1] * len(input_ids)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        # input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        while len(input_ids) < max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)
            label_seq.append(0)

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length
        assert len(label_seq) == max_seq_length

        if example_index < 20:
            logger.info("*** Example ***")
            logger.info("unique_id: %s" % (unique_id))
            logger.info("example_index: %s" % (example_index))
            logger.info("tokens: %s" % " ".join(tokens))
            logger.info("token_to_orig_map: %s" % " ".join([
                "%d:%d" % (x, y) for (x, y) in token_to_orig_map.items()]))
            logger.info("token_is_max_context: %s" % " ".join([
                "%d:%s" % (x, y) for (x, y) in token_is_max_context.items()
            ]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info(
                "input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info(
                "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info(
                "label seq: %s" % " ".join([str(x) for x in label_seq]))

        features.append(
            InputFeatures(
                unique_id=unique_id,
                # the index of the example
                example_index=example_index,
                # the sequence of the sub-tokens with tags
                tokens=tokens,
                # the list record of original index for each item in tokens
                token_to_orig_map=token_to_orig_map,
                # id sequence for the sub-tokens with tags / the conversion from tokens
                input_ids=input_ids,
                # indicate which position is the real word or tag
                input_mask=input_mask,
                # indicate which position is the sequence 1, useless
                segment_ids=segment_ids,
                # labels for each words, and 0 indicates tags or None-word
                label_seq=label_seq))
        unique_id += 1

    return features


RawResult = collections.namedtuple("RawResult",
                                   ["unique_id", "logits"])


def get_final_text(pred_text, orig_text, do_lower_case, verbose_logging=False):
    """Project the tokenized prediction back to the original text."""

    # When we created the data, we kept track of the alignment between original
    # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
    # now `orig_text` contains the span of our original text corresponding to the
    # span that we predicted.
    #
    # However, `orig_text` may contain extra characters that we don't want in
    # our prediction.
    #
    # For example, let's say:
    #   pred_text = steve smith
    #   orig_text = Steve Smith's
    #
    # We don't want to return `orig_text` because it contains the extra "'s".
    #
    # We don't want to return `pred_text` because it's already been normalized
    # (the SQuAD eval script also does punctuation stripping/lower casing but
    # our tokenizer does additional normalization like stripping accent
    # characters).
    #
    # What we really want to return is "Steve Smith".
    #
    # Therefore, we have to apply a semi-complicated alignment heruistic between
    # `pred_text` and `orig_text` to get a character-to-charcter alignment. This
    # can fail in certain cases in which case we just return `orig_text`.

    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return (ns_text, ns_to_s_map)

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.
    tokenizer = BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose_logging:
            logger.info(
                "Unable to find text: '%s' in '%s'" % (pred_text, orig_text))
        return orig_text
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose_logging:
            logger.info("Length not equal after stripping spaces: '%s' vs '%s'",
                            orig_ns_text, tok_ns_text)
        return orig_text

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in tok_ns_to_s_map.items():
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose_logging:
            logger.info("Couldn't map start position")
        return orig_text

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose_logging:
            logger.info("Couldn't map end position")
        return orig_text

    output_text = orig_text[orig_start_position:(orig_end_position + 1)]
    return output_text


def write_predictions(all_examples, all_features, all_results, n_best_size,
                      max_answer_length, do_lower_case, output_prediction_file,
                      output_nbest_file, verbose_logging):
    """Write final predictions to the json file."""
    logger.info("Writing predictions to: %s" % (output_prediction_file))
    logger.info("Writing nbest to: %s" % (output_nbest_file))

    example_index_to_features = collections.defaultdict(list)
    for feature in all_features:
        example_index_to_features[feature.example_index].append(feature)

    unique_id_to_result = {}
    for result in all_results:
        unique_id_to_result[result.unique_id] = result

    _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
        "PrelimPrediction",
        ["feature_index", "start_index", "end_index", "start_logit", "end_logit"])

    all_predictions = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()
    for (example_index, example) in enumerate(all_examples):
        features = example_index_to_features[example_index]

        prelim_predictions = []
        for (feature_index, feature) in enumerate(features):
            result = unique_id_to_result[feature.unique_id]

            start_indexes = _get_best_indexes(result.start_logits, n_best_size)
            end_indexes = _get_best_indexes(result.end_logits, n_best_size)
            for start_index in start_indexes:
                for end_index in end_indexes:
                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= len(feature.tokens):
                        continue
                    if end_index >= len(feature.tokens):
                        continue
                    if start_index not in feature.token_to_orig_map:
                        continue
                    if end_index not in feature.token_to_orig_map:
                        continue
                    if not feature.token_is_max_context.get(start_index, False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue
                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            start_logit=result.start_logits[start_index],
                            end_logit=result.end_logits[end_index]))

        prelim_predictions = sorted(
            prelim_predictions,
            key=lambda x: (x.start_logit + x.end_logit),
            reverse=True)

        _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
            "NbestPrediction", ["text", "start_logit", "end_logit"])

        seen_predictions = {}
        nbest = []
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            feature = features[pred.feature_index]

            tok_tokens = feature.tokens[pred.start_index:(pred.end_index + 1)]
            orig_doc_start = feature.token_to_orig_map[pred.start_index]
            orig_doc_end = feature.token_to_orig_map[pred.end_index]
            orig_tokens = example.doc_tokens[orig_doc_start:(orig_doc_end + 1)]
            tok_text = " ".join(tok_tokens)

            # De-tokenize WordPieces that have been split off.
            tok_text = tok_text.replace(" ##", "")
            tok_text = tok_text.replace("##", "")

            # Clean whitespace
            tok_text = tok_text.strip()
            tok_text = " ".join(tok_text.split())
            orig_text = " ".join(orig_tokens)

            final_text = get_final_text(tok_text, orig_text, do_lower_case, verbose_logging)
            if final_text in seen_predictions:
                continue

            seen_predictions[final_text] = True
            nbest.append(
                _NbestPrediction(
                    text=final_text,
                    start_logit=pred.start_logit,
                    end_logit=pred.end_logit))

        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(
                _NbestPrediction(text="empty", start_logit=0.0, end_logit=0.0))

        assert len(nbest) >= 1

        total_scores = []
        for entry in nbest:
            total_scores.append(entry.start_logit + entry.end_logit)

        probs = _compute_softmax(total_scores)

        nbest_json = []
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            nbest_json.append(output)

        assert len(nbest_json) >= 1

        all_predictions[example.qas_id] = nbest_json[0]["text"]
        all_nbest_json[example.qas_id] = nbest_json

    with open(output_prediction_file, "w") as writer:
        writer.write(json.dumps(all_predictions, indent=4) + "\n")

    with open(output_nbest_file, "w") as writer:
        writer.write(json.dumps(all_nbest_json, indent=4) + "\n")


def _get_best_indexes(logits, n_best_size):
    """Get the n-best logits from a list."""
    index_and_score = sorted(enumerate(logits), key=lambda x: x[1], reverse=True)

    best_indexes = []
    for i in range(len(index_and_score)):
        if i >= n_best_size:
            break
        best_indexes.append(index_and_score[i][0])
    return best_indexes


def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs


def accuracy(out, labels):
    outputs = np.argmax(out, axis=1)
    return np.sum(outputs == labels)


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                        "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                        "bert-base-multilingual-cased, bert-base-chinese.")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model checkpoints and predictions will be written.")

    ## Other parameters
    parser.add_argument("--train_file", default=None, type=str, help="SQuAD json for training. E.g., train-v1.1.json")
    parser.add_argument("--dev_file", default=None, type=str, help="SQuAD json for training. E.g., train-v1.1.json")
    parser.add_argument("--test_file", default=None, type=str, help="SQuAD json for training. E.g., train-v1.1.json")
    parser.add_argument("--predict_file", default=None, type=str,
                        help="SQuAD json for predictions. E.g., dev-v1.1.json or test-v1.1.json")
    parser.add_argument("--max_seq_length", default=384, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. Sequences "
                             "longer than this will be truncated, and sequences shorter than this will be padded.")
    parser.add_argument("--doc_stride", default=128, type=int,
                        help="When splitting up a long document into chunks, how much stride to take between chunks.")
    parser.add_argument("--max_query_length", default=64, type=int,
                        help="The maximum number of tokens for the question. Questions longer than this will "
                             "be truncated to this length.")
    parser.add_argument("--do_train", action='store_true', help="Whether to run training.")
    parser.add_argument("--do_eval",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_test",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_predict", action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--train_batch_size", default=32, type=int, help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--test_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--predict_batch_size", default=8, type=int, help="Total batch size for predictions.")
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion", default=0.1, type=float,
                        help="Proportion of training to perform linear learning rate warmup for. E.g., 0.1 = 10%% "
                             "of training.")
    parser.add_argument("--n_best_size", default=20, type=int,
                        help="The total number of n-best predictions to generate in the nbest_predictions.json "
                             "output file.")
    parser.add_argument("--max_answer_length", default=30, type=int,
                        help="The maximum length of an answer that can be generated. This is needed because the start "
                             "and end predictions are not conditioned on one another.")
    parser.add_argument("--verbose_logging", action='store_true',
                        help="If true, all of the warnings related to data processing will be printed. "
                             "A number of warnings are expected for a normal SQuAD evaluation.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Whether to lower case the input text. True for uncased models, False for cased models.")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")
    parser.add_argument('--version_2_with_negative',
                        action='store_true',
                        help='If true, the SQuAD examples contain some that do not have an answer.')
    parser.add_argument('--null_score_diff_threshold',
                        type=float, default=0.0,
                        help="If null_score - best_non_null is greater than the threshold predict null.")
    args = parser.parse_args()

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_predict:
        raise ValueError("At least one of `do_train` or `do_predict` must be True.")

    if args.do_train:
        if not args.train_file:
            raise ValueError(
                "If `do_train` is True, then `train_file` must be specified.")
    if args.do_predict:
        if not args.predict_file:
            raise ValueError(
                "If `do_predict` is True, then `predict_file` must be specified.")

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train:
        raise ValueError("Output directory () already exists and is not empty.")
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_optimization_steps = None
    if args.do_train:
        train_examples = read_label_examples(
            input_file=args.train_file, is_training=True)
        num_train_optimization_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps) * args.num_train_epochs
        if args.local_rank != -1:
            num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()

    if args.do_eval:
        eval_examples = read_label_examples(input_file=args.dev_file, is_training=True)
        eval_features = convert_examples_to_features(
            examples=eval_examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_insertion_length=args.max_query_length,
            is_training=True)

    # Prepare model
    model = BertForLabelling.from_pretrained(args.bert_model)
    # model = BertForQuestionAnswering.from_pretrained(args.bert_model,
    #             cache_dir=os.path.join(PYTORCH_PRETRAINED_BERT_CACHE, 'distributed_{}'.format(args.local_rank)))

    if args.fp16:
        model.half()
    model.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        model = DDP(model)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())

    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]

    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

    if args.fp16:
        try:
            from apex.optimizers import FP16_Optimizer
            from apex.optimizers import FusedAdam
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        optimizer = FusedAdam(optimizer_grouped_parameters,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)
        if args.loss_scale == 0:
            optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
        else:
            optimizer = FP16_Optimizer(optimizer, static_loss_scale=args.loss_scale)
    else:
        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=num_train_optimization_steps)

    global_step = 0
    best_acc = 0
    if args.do_train:
        cached_train_features_file = args.train_file+'_{0}_{1}_{2}_{3}'.format(
            list(filter(None, args.bert_model.split(os.sep))).pop(), str(args.max_seq_length), str(args.doc_stride), str(args.max_query_length))
        train_features = None
        try:
            with open(cached_train_features_file, "rb") as reader:
                train_features = pickle.load(reader)
        except:
            train_features = convert_examples_to_features(
                examples=train_examples,
                tokenizer=tokenizer,
                max_seq_length=args.max_seq_length,
                doc_stride=args.doc_stride,
                max_insertion_length=args.max_query_length,
                is_training=True)
            if args.local_rank == -1 or torch.distributed.get_rank() == 0:
                logger.info("  Saving train features into cached file %s", cached_train_features_file)
                with open(cached_train_features_file, "wb") as writer:
                    pickle.dump(train_features, writer)
        logger.info("***** Running training *****")
        logger.info("  Num orig examples = %d", len(train_examples))
        logger.info("  Num split examples = %d", len(train_features))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)

        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)
        all_label_seq = torch.tensor([f.label_seq for f in train_features], dtype=torch.float)

        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                                   all_label_seq)

        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        model.train()
        for epoch in range(int(args.num_train_epochs)):
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                if n_gpu == 1:
                    batch = tuple(t.to(device) for t in batch) # multi-gpu does scattering it-self
                input_ids, input_mask, segment_ids, label_seq = batch
                loss = model(input_ids, segment_ids, input_mask, label_seq)
                if n_gpu > 1:
                    loss = loss.mean() # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    if args.fp16:
                        # modify learning rate with special warm up BERT uses
                        # if args.fp16 is False, BertAdam is used and handles this automatically
                        lr_this_step = args.learning_rate * warmup_linear(global_step/num_train_optimization_steps, args.warmup_proportion)
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = lr_this_step
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1

            if args.do_eval:
                logger.info("***** Running evaluation %d *****", epoch)
                logger.info("  Num examples = %d", len(eval_examples))
                logger.info("  Batch size = %d", args.eval_batch_size)
                all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
                all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
                all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
                all_label_seq = torch.tensor([f.label_seq for f in eval_features], dtype=torch.float)
                eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_seq)
                # Run prediction for full data
                eval_sampler = SequentialSampler(eval_data)
                eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

                model.eval()
                eval_loss, eval_accuracy = 0, 0
                nb_eval_steps, nb_eval_examples = 0, 0

                for input_ids, input_mask, segment_ids, label_ids in tqdm(eval_dataloader, desc="Evaluating"):
                    input_ids = input_ids.to(device)
                    input_mask = input_mask.to(device)
                    segment_ids = segment_ids.to(device)
                    label_ids = label_ids.to(device)

                    with torch.no_grad():
                        tmp_eval_loss = model(input_ids, segment_ids, input_mask, label_ids)
                        logits = model(input_ids, segment_ids, input_mask)

                    logits = logits.detach().cpu().numpy()
                    label_ids = label_ids.to('cpu').numpy()

                    tem_logits = torch.tensor([pos[1] for item in logits for pos in item],
                                              dtype=torch.float).view(-1, args.max_seq_length)
                    tem_logits = tem_logits.detach().cpu().numpy()
                    tmp_eval_accuracy = accuracy(np.array(tem_logits), np.argmax(np.array(label_ids), 1))

                    eval_loss += tmp_eval_loss.mean().item()
                    eval_accuracy += tmp_eval_accuracy

                    nb_eval_examples += input_ids.size(0)
                    nb_eval_steps += 1

                eval_loss = eval_loss / nb_eval_steps
                eval_accuracy = eval_accuracy / nb_eval_examples
                # loss = tr_loss / nb_tr_steps if args.do_train else None
                result = {'eval_loss': eval_loss,
                          'eval_accuracy': eval_accuracy,
                          'global_step': global_step}

                if eval_accuracy > best_acc:
                    best_acc = eval_accuracy
                    output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
                    with open(output_eval_file, "w") as writer:
                        logger.info("***** Eval results *****")
                        for key in sorted(result.keys()):
                            logger.info("  %s = %s", key, str(result[key]))
                            writer.write("%s = %s\n" % (key, str(result[key])))

                    model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                    output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
                    torch.save(model_to_save.state_dict(), output_model_file)
                    output_config_file = os.path.join(args.output_dir, CONFIG_NAME)
                    with open(output_config_file, 'w') as f:
                        f.write(model_to_save.config.to_json_string())

    if args.do_test:
        test_examples = read_label_examples(input_file=args.test_file, is_training=True)
        test_features = convert_examples_to_features(
            examples=test_examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_insertion_length=args.max_query_length,
            is_training=True)

        logger.info("***** Running test evaluation *****")
        logger.info("  Num examples = %d", len(test_examples))
        logger.info("  Test Batch size = %d", args.test_batch_size)
        all_input_ids = torch.tensor([f.input_ids for f in test_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in test_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in test_features], dtype=torch.long)
        all_label_seq = torch.tensor([f.label_seq for f in test_features], dtype=torch.float)
        test_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_seq)
        # Run prediction for full data
        test_sampler = SequentialSampler(test_data)
        test_dataloader = DataLoader(test_data, sampler=test_sampler, batch_size=args.test_batch_size)
        model.eval()
        test_loss, test_accuracy = 0, 0
        nb_test_steps, nb_test_examples = 0, 0

        for input_ids, input_mask, segment_ids, label_ids in tqdm(test_dataloader, desc="Testing"):
            input_ids = input_ids.to(device)
            input_mask = input_mask.to(device)
            segment_ids = segment_ids.to(device)
            label_ids = label_ids.to(device)

            with torch.no_grad():
                tmp_test_loss = model(input_ids, segment_ids, input_mask, label_ids)
                logits = model(input_ids, segment_ids, input_mask)

            logits = logits.detach().cpu().numpy()
            label_ids = label_ids.to('cpu').numpy()

            tem_logits = torch.tensor([pos[1] for item in logits for pos in item], dtype=torch.float).view(-1,
                                                                                                                    args.max_seq_length)
            tem_logits = tem_logits.detach().cpu().numpy()
            tmp_test_accuracy = accuracy(np.array(tem_logits), np.argmax(np.array(label_ids), 1))

            test_loss += tmp_test_loss.mean().item()
            test_accuracy += tmp_test_accuracy

            nb_test_examples += input_ids.size(0)
            nb_test_steps += 1

        test_loss = test_loss / nb_test_steps
        test_accuracy = test_accuracy / nb_test_examples
        # loss = tr_loss / nb_tr_steps if args.do_train else None
        result = {'test_loss': test_loss,
                  'test_accuracy': test_accuracy,
                  'global_step': global_step}
        output_test_file = os.path.join(args.output_dir, "test_results.txt")
        with open(output_test_file, "w") as writer:
            logger.info("***** Test results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))


















    # if args.do_train:
    #     # Save a trained model and the associated configuration
    #     model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
    #     output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
    #     torch.save(model_to_save.state_dict(), output_model_file)
    #     output_config_file = os.path.join(args.output_dir, CONFIG_NAME)
    #     with open(output_config_file, 'w') as f:
    #         f.write(model_to_save.config.to_json_string())
    #
    #     # Load a trained model and config that you have fine-tuned
    #     config = BertConfig(output_config_file)
    #     model = BertForLabelling(config)
    #     model.load_state_dict(torch.load(output_model_file))
    # else:
    #     model = BertForLabelling.from_pretrained(args.bert_model)
    #
    # model.to(device)

    if args.do_predict and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        test_examples = read_label_examples(
            input_file=args.predict_file, is_training=False)
        test_features = convert_examples_to_features(
            examples=test_examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_insertion_length=args.max_query_length,
            is_training=False)

        logger.info("***** Running predictions *****")
        logger.info("  Num orig examples = %d", len(test_examples))
        logger.info("  Num split examples = %d", len(test_features))
        logger.info("  Batch size = %d", args.predict_batch_size)

        all_input_ids = torch.tensor([f.input_ids for f in test_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in test_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in test_features], dtype=torch.long)
        all_example_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
        test_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_example_index)
        # Run prediction for full data
        test_sampler = SequentialSampler(test_data)
        test_dataloader = DataLoader(test_data, sampler=test_sampler, batch_size=args.predict_batch_size)

        model.eval()
        all_results = []
        logger.info("Start evaluating")
        nb_f1_scores, nb_nums = 0, 0
        nb_p_scores, nb_r_scores = 0, 0
        output_prediction_file = os.path.join(args.output_dir, "predictions.txt")
        output_prediction_file_writer = open(output_prediction_file, 'w', encoding='utf-8')
        for input_ids, input_mask, segment_ids, example_indices in tqdm(test_dataloader, desc="Evaluating"):
            if len(all_results) % 1000 == 0:
                logger.info("Processing example: %d" % (len(all_results)))
            input_ids = input_ids.to(device)
            input_mask = input_mask.to(device)
            segment_ids = segment_ids.to(device)
            with torch.no_grad():
                batch_logits = model(input_ids, segment_ids, input_mask)
            for i, example_index in enumerate(example_indices):
                # for each instance
                logits = batch_logits[i].detach().cpu().tolist()
                # the input features for the instance
                eval_feature = test_features[example_index.item()]
                # get the predict location / the index of the max value
                predict_label = np.argmax(np.array(logits), 1)

                nb_f1_scores += f1_score(predict_label, np.array(eval_feature.label_seq))
                nb_p_scores += precision_score(predict_label, np.array(eval_feature.label_seq))
                nb_r_scores += recall_score(predict_label, np.array(eval_feature.label_seq))

                nb_nums += 1
                words = test_examples[example_index.item()].doc_tokens
                insertion = test_examples[example_index.item()].insertion
                words_scores = [-1] * (len(words) + 1)
                for token_id in range(len(logits)):
                    if token_id not in eval_feature.token_to_orig_map:
                        continue
                    orig_id = eval_feature.token_to_orig_map[token_id]
                    words_scores[orig_id] = max(words_scores[orig_id], logits[token_id][1] - logits[token_id][0])

                words_num = max(eval_feature.token_to_orig_map.values()) + 1
                output_prediction_file_writer.write(' '.join(words[:words_num]))
                output_prediction_file_writer.write('\t')
                output_prediction_file_writer.write(insertion)
                output_prediction_file_writer.write('\t')
                # output_prediction_file_writer.write(' '.join([str(ws) for ws in words_scores[:words_num]]))
                output_prediction_file_writer.write(str(np.argmax([ws for ws in words_scores[:words_num]])))
                output_prediction_file_writer.write('\n')

        print(nb_f1_scores / nb_nums)
        print(nb_p_scores / nb_nums)
        print(nb_r_scores / nb_nums)

        output_result_file = os.path.join(args.output_dir, "result.txt")
        open(output_result_file, 'w', encoding='utf-8').write(str(nb_f1_scores / nb_nums))
        open(output_result_file, 'a', encoding='utf-8').write(str(nb_p_scores / nb_nums))
        open(output_result_file, 'a', encoding='utf-8').write(str(nb_r_scores / nb_nums))


if __name__ == "__main__":
    start = datetime.now()
    main()
    end = datetime.now()
    print(end-start)
