# code adapted from https://github.com/huggingface/transformers/tree/master/examples/pytorch/text-classification

import argparse
import logging
import math
import os
import random

import datasets
from datasets import load_dataset, load_metric
import torch
from torch.utils.data.dataloader import DataLoader
from tqdm.auto import tqdm

import transformers
from transformers import (
    AdamW,
    AutoConfig,
    AutoModel,
    AutoModelForPreTraining,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    PretrainedConfig,
    SchedulerType,
    default_data_collator,
    get_scheduler,
    set_seed,
)

import nni
from nni.compression.pytorch import ModelSpeedup
from nni.compression.pytorch.utils.counter import count_flops_params
from nni.algorithms.compression.pytorch.pruning import (
    TransformerHeadPruner
)

logger = logging.getLogger('bert_pruning_example')

task_to_keys = {
    "cola": ("sentence", None),
    "mnli": ("premise", "hypothesis"),
    "mrpc": ("sentence1", "sentence2"),
    "qnli": ("question", "sentence"),
    "qqp": ("question1", "question2"),
    "rte": ("sentence1", "sentence2"),
    "sst2": ("sentence", None),
    "stsb": ("sentence1", "sentence2"),
    "wnli": ("sentence1", "sentence2"),
}


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune a transformers model on a text classification task")

    parser.add_argument("--model_name_or_path", type=str, required=True,
                        help="Path to pretrained model or model identifier from huggingface.co/models.")
    parser.add_argument("--task_name", type=str, default=None,
                        help="The name of the glue task to train on.",
                        choices=list(task_to_keys.keys()))
    parser.add_argument("--output_dir", type=str, default=None,
                        help="Where to store the final model.")
    parser.add_argument('--usage', type=int, default=1,
                        help='Select which config example to run')
    parser.add_argument('--sparsity', type=float, required=True,
                        help='Sparsity - proportion of heads to prune (should be between 0 and 1)')
    parser.add_argument('--global_sort', action='store_true', default=False,
                        help='Rank the heads globally and prune the heads with lowest scores. If set to False, the '
                             'heads are only ranked within one layer')
    parser.add_argument("--ranking_criterion", type=str, default='l1_weight',
                        choices=["l1_weight", "l2_weight", "l1_activation", "l2_activation", "taylorfo"],
                        help="Where to store the final model.")
    parser.add_argument("--num_iterations", type=int, default=1,
                        help="Number of pruning iterations (1 for one-shot pruning).")
    parser.add_argument("--epochs_per_iteration", type=int, default=1,
                        help="Epochs to finetune before the next pruning iteration "
                             "(only effective if num_iterations > 1).")
    parser.add_argument('--speed_up', action='store_true', default=False,
                        help='Whether to speed-up the pruned model')

    # parameters for model training; for running examples. no need to change them
    parser.add_argument("--train_file", type=str, default=None,
                        help="A csv or a json file containing the training data.")
    parser.add_argument("--validation_file", type=str, default=None,
                        help="A csv or a json file containing the validation data.")
    parser.add_argument("--max_length", type=int, default=128,
                        help=("The maximum total input sequence length after tokenization. Sequences longer than this "
                              "will be truncated, sequences shorter will be padded if `--pad_to_max_lengh` is passed."))
    parser.add_argument("--pad_to_max_length", action="store_true",
                        help="If passed, pad all samples to `max_length`. Otherwise, dynamic padding is used.")
    parser.add_argument("--use_slow_tokenizer", action="store_true",
                        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",)
    parser.add_argument("--per_device_train_batch_size", type=int, default=8,
                        help="Batch size (per device) for the training dataloader.")
    parser.add_argument("--per_device_eval_batch_size", type=int, default=8,
                        help="Batch size (per device) for the evaluation dataloader.")
    parser.add_argument("--learning_rate", type=float, default=5e-5,
                        help="Initial learning rate (after the potential warmup period) to use.")
    parser.add_argument("--weight_decay", type=float, default=0.0,
                        help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=3,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_train_steps", type=int, default=None,
                        help="Total number of training steps to perform. If provided, overrides num_train_epochs.")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--lr_scheduler_type", type=SchedulerType, default="linear",
                        help="The scheduler type to use.",
                        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant",
                                 "constant_with_warmup"])
    parser.add_argument("--num_warmup_steps", type=int, default=0,
                        help="Number of steps for the warmup in the lr scheduler.")
    parser.add_argument("--seed", type=int, default=None,
                        help="A seed for reproducible training.")

    args = parser.parse_args()

    # Sanity checks
    if args.task_name is None and args.train_file is None and args.validation_file is None:
        raise ValueError("Need either a task name or a training/validation file.")
    else:
        if args.train_file is not None:
            extension = args.train_file.split(".")[-1]
            assert extension in ["csv", "json"], "`train_file` should be a csv or a json file."
        if args.validation_file is not None:
            extension = args.validation_file.split(".")[-1]
            assert extension in ["csv", "json"], "`validation_file` should be a csv or a json file."

    if args.output_dir is not None:
        os.makedirs(args.output_dir, exist_ok=True)

    return args


def get_raw_dataset(args):
    # Get the datasets: you can either provide your own CSV/JSON training and evaluation files (see below)
    # or specify a GLUE benchmark task (the dataset will be downloaded automatically from the datasets Hub).

    # For CSV/JSON files, this script will use as labels the column called 'label' and as pair of sentences the
    # sentences in columns called 'sentence1' and 'sentence2' if such column exists or the first two columns not named
    # label if at least two columns are provided.

    # If the CSVs/JSONs contain only one non-label column, the script does single sentence classification on this
    # single column. You can easily tweak this behavior (see below)

    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    if args.task_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_dataset("glue", args.task_name)
    else:
        # Loading the dataset from local csv or json file.
        data_files = {}
        if args.train_file is not None:
            data_files["train"] = args.train_file
        if args.validation_file is not None:
            data_files["validation"] = args.validation_file
        extension = (args.train_file if args.train_file is not None else args.valid_file).split(".")[-1]
        raw_datasets = load_dataset(extension, data_files=data_files)
    # See more about loading any type of standard or custom dataset at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Labels
    if args.task_name is not None:
        is_regression = args.task_name == "stsb"
        if not is_regression:
            label_list = raw_datasets["train"].features["label"].names
            num_labels = len(label_list)
        else:
            label_list = None
            num_labels = 1
    else:
        # Trying to have good defaults here, don't hesitate to tweak to your needs.
        is_regression = raw_datasets["train"].features["label"].dtype in ["float32", "float64"]
        if is_regression:
            label_list = None
            num_labels = 1
        else:
            # A useful fast method:
            # https://huggingface.co/docs/datasets/package_reference/main_classes.html#datasets.Dataset.unique
            label_list = raw_datasets["train"].unique("label")
            label_list.sort()  # Let's sort it for determinism
            num_labels = len(label_list)

    return raw_datasets, is_regression, label_list, num_labels


def preprocess_dataset(args, tokenizer, model, raw_datasets, num_labels, is_regression, label_list):
    # Preprocessing the datasets
    if args.task_name is not None:
        sentence1_key, sentence2_key = task_to_keys[args.task_name]
    else:
        # Again, we try to have some nice defaults but don't hesitate to tweak to your use case.
        non_label_column_names = [name for name in raw_datasets["train"].column_names if name != "label"]
        if "sentence1" in non_label_column_names and "sentence2" in non_label_column_names:
            sentence1_key, sentence2_key = "sentence1", "sentence2"
        else:
            if len(non_label_column_names) >= 2:
                sentence1_key, sentence2_key = non_label_column_names[:2]
            else:
                sentence1_key, sentence2_key = non_label_column_names[0], None

    # Some models have set the order of the labels to use, so let's make sure we do use it.
    label_to_id = None
    if (
            model.config.label2id != PretrainedConfig(num_labels=num_labels).label2id
            and args.task_name is not None
            and not is_regression
    ):
        # Some have all caps in their config, some don't.
        label_name_to_id = {k.lower(): v for k, v in model.config.label2id.items()}
        if list(sorted(label_name_to_id.keys())) == list(sorted(label_list)):
            logger.info(
                f"The configuration of the model provided the following label correspondence: {label_name_to_id}. "
                "Using it!"
            )
            label_to_id = {i: label_name_to_id[label_list[i]] for i in range(num_labels)}
        else:
            logger.warning(
                "Your model seems to have been trained with labels, but they don't match the dataset: ",
                f"model labels: {list(sorted(label_name_to_id.keys()))}, dataset labels: {list(sorted(label_list))}."
                "\nIgnoring the model labels as a result.",
            )
    elif args.task_name is None:
        label_to_id = {v: i for i, v in enumerate(label_list)}

    padding = "max_length" if args.pad_to_max_length else False

    def preprocess_function(examples):
        # Tokenize the texts
        texts = (
            (examples[sentence1_key],) if sentence2_key is None else (examples[sentence1_key], examples[sentence2_key])
        )
        result = tokenizer(*texts, padding=padding, max_length=args.max_length, truncation=True)

        if "label" in examples:
            if label_to_id is not None:
                # Map labels to IDs (not necessary for GLUE tasks)
                result["labels"] = [label_to_id[l] for l in examples["label"]]
            else:
                # In all cases, rename the column to labels because the model will expect that.
                result["labels"] = examples["label"]
        return result

    processed_datasets = raw_datasets.map(
        preprocess_function, batched=True, remove_columns=raw_datasets["train"].column_names
    )
    return processed_datasets


def train_model(args, model, is_regression, train_dataloader, eval_dataloader, optimizer, lr_scheduler, metric, device,
                epoch_num=None):
    progress_bar = tqdm(range(args.max_train_steps), position=0, leave=True)
    completed_steps = 0

    train_epoch = args.num_train_epochs if epoch_num is None else 1
    for epoch in range(train_epoch):
        model.train()
        for step, batch in enumerate(train_dataloader):
            for field in batch.keys():
                batch[field] = batch[field].to(device)
            outputs = model(**batch)
            loss = outputs.loss
            loss = loss / args.gradient_accumulation_steps
            loss.backward()
            if step % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()
                progress_bar.update(1)
                completed_steps += 1

            if completed_steps >= args.max_train_steps:
                break

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            for field in batch.keys():
                batch[field] = batch[field].to(device)
            outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1) if not is_regression else outputs.logits.squeeze()
            metric.add_batch(
                predictions=predictions,
                references=batch["labels"],
            )

        eval_metric = metric.compute()
        logger.info(f"epoch {epoch}: {eval_metric}")


def dry_run_or_finetune(args, model, train_dataloader, optimizer, device, epoch_num=None):
    if epoch_num == 0:
        print("Running forward and backward on the entire dataset without updating parameters...")
    else:
        print("Finetuning for 1 epoch")
    progress_bar = tqdm(range(len(train_dataloader)), position=0, leave=True)
    completed_steps = 0

    train_epoch = args.num_train_epochs if epoch_num is None else 1
    for epoch in range(train_epoch):
        for step, batch in enumerate(train_dataloader):
            for field in batch.keys():
                batch[field] = batch[field].to(device)
            outputs = model(**batch)
            loss = outputs.loss
            loss.backward()
            if epoch_num != 0:
                optimizer.step()
            optimizer.zero_grad()
            progress_bar.update(1)
            completed_steps += 1


def final_eval_for_mnli(args, model, processed_datasets, metric, data_collator):
    # Final evaluation on mismatched validation set
    eval_dataset = processed_datasets["validation_mismatched"]
    eval_dataloader = DataLoader(
        eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
    )

    model.eval()
    for step, batch in enumerate(eval_dataloader):
        outputs = model(**batch)
        predictions = outputs.logits.argmax(dim=-1)
        metric.add_batch(
            predictions=predictions,
            references=batch["labels"],
        )

    eval_metric = metric.compute()
    logger.info(f"mnli-mm: {eval_metric}")


def get_dataloader_and_optimizer(args, tokenizer, model, train_dataset, eval_dataset):
    # DataLoaders creation:
    if args.pad_to_max_length:
        # If padding was already done ot max length, we use the default data collator that will just convert everything
        # to tensors.
        data_collator = default_data_collator
    else:
        # Otherwise, `DataCollatorWithPadding` will apply dynamic padding for us (by padding to the maximum length of
        # the samples passed). When using mixed precision, we add `pad_to_multiple_of=8` to pad all tensors to multiple
        # of 8s, which will enable the use of Tensor Cores on NVIDIA hardware with compute capability >= 7.5 (Volta).
        data_collator = DataCollatorWithPadding(tokenizer)

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=data_collator, batch_size=args.per_device_train_batch_size
    )
    eval_dataloader = DataLoader(eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size)

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

    return model, optimizer, train_dataloader, eval_dataloader, data_collator


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args = parse_args()

    #########################################################################
    # Prepare model, tokenizer, dataset, optimizer, and the scheduler
    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.setLevel(logging.INFO)
    datasets.utils.logging.set_verbosity_warning()
    transformers.utils.logging.set_verbosity_info()

    if args.seed is not None:
        set_seed(args.seed)

    raw_datasets, is_regression, label_list, num_labels = get_raw_dataset(args)

    # Load pretrained model and tokenizer
    config = AutoConfig.from_pretrained(args.model_name_or_path, num_labels=num_labels, finetuning_task=args.task_name)
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, use_fast=not args.use_slow_tokenizer)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name_or_path,
        from_tf=bool(".ckpt" in args.model_name_or_path),
        config=config,
    )
    model.to(device)

    processed_datasets = preprocess_dataset(args, tokenizer, model, raw_datasets, num_labels, is_regression, label_list)
    train_dataset = processed_datasets["train"]
    eval_dataset = processed_datasets["validation_matched" if args.task_name == "mnli" else "validation"]

    #########################################################################
    # Finetune on the target GLUE task before pruning
    model, optimizer, train_dataloader, eval_dataloader, data_collator = get_dataloader_and_optimizer(args, tokenizer,
                                                                                                      model,
                                                                                                      train_dataset,
                                                                                                      eval_dataset)

    # Scheduler and math around the number of training steps.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    else:
        args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps,
        num_training_steps=args.max_train_steps,
    )

    # Get the metric function
    if args.task_name is not None:
        metric = load_metric("glue", args.task_name)
    else:
        metric = load_metric("accuracy")

    total_batch_size = args.per_device_train_batch_size * args.gradient_accumulation_steps
    logger.info("***** Finetuning before pruning *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    train_model(args, model, is_regression, train_dataloader, eval_dataloader, optimizer, lr_scheduler, metric, device)

    if args.output_dir is not None:
        torch.save(model.state_dict(), args.output_dir + '/model_before_pruning.pt')

    if args.task_name == "mnli":
        final_eval_for_mnli(args, model, processed_datasets, metric, data_collator)

    #########################################################################
    # Pruning
    model, optimizer, train_dataloader, eval_dataloader, data_collator = get_dataloader_and_optimizer(args, tokenizer,
                                                                                                      model,
                                                                                                      train_dataset,
                                                                                                      eval_dataset)
    dummy_input = next(iter(train_dataloader))['input_ids'].to(device)
    flops, params, results = count_flops_params(model, dummy_input)
    print(f'Initial model FLOPs {flops / 1e6:.2f} M, #Params: {params / 1e6:.2f}M')

    # here criterion is embedded in the model. Upper levels can just pass None to trainer
    def trainer(model, optimizer, criterion, epoch):
        return dry_run_or_finetune(args, model, train_dataloader, optimizer, device, epoch_num=epoch)

    # We provide three usages, set the "usage" parameter in the command line argument to run one of them.
    # example 1: prune all layers with uniform sparsity
    if args.usage == 1:
        kwargs = {'ranking_criterion': args.ranking_criterion,
                  'global_sort': args.global_sort,
                  'num_iterations': args.num_iterations,
                  'epochs_per_iteration': args.epochs_per_iteration,
                  'head_hidden_dim': 64,
                  'dummy_input': dummy_input,
                  'trainer': trainer,
                  'optimizer': optimizer}

        config_list = [{
            'sparsity': args.sparsity,
            'op_types': ["Linear"],
        }]

    # example 2: prune different layers with uniform sparsity, but specify names group instead of dummy_input
    elif args.usage == 2:
        attention_name_groups = list(zip(['encoder.layer.{}.attention.self.query'.format(i) for i in range(12)],
                                         ['encoder.layer.{}.attention.self.key'.format(i) for i in range(12)],
                                         ['encoder.layer.{}.attention.self.value'.format(i) for i in range(12)],
                                         ['encoder.layer.{}.attention.output.dense'.format(i) for i in range(12)]))

        kwargs = {'ranking_criterion': args.ranking_criterion,
                  'global_sort': args.global_sort,
                  'num_iterations': args.num_iterations,
                  'epochs_per_iteration': args.epochs_per_iteration,
                  'attention_name_groups': attention_name_groups,
                  'head_hidden_dim': 64,
                  'trainer': trainer,
                  'optimizer': optimizer}

        config_list = [{
            'sparsity': args.sparsity,
            'op_types': ["Linear"],
            'op_names': [x for layer in attention_name_groups for x in layer]
        }
        ]

    # example 3: prune different layers with different sparsity
    elif args.usage == 3:
        attention_name_groups = list(zip(['encoder.layer.{}.attention.self.query'.format(i) for i in range(12)],
                                         ['encoder.layer.{}.attention.self.key'.format(i) for i in range(12)],
                                         ['encoder.layer.{}.attention.self.value'.format(i) for i in range(12)],
                                         ['encoder.layer.{}.attention.output.dense'.format(i) for i in range(12)]))

        kwargs = {'ranking_criterion': args.ranking_criterion,
                  'global_sort': args.global_sort,
                  'num_iterations': args.num_iterations,
                  'epochs_per_iteration': args.epochs_per_iteration,
                  'attention_name_groups': attention_name_groups,
                  'head_hidden_dim': 64,
                  'trainer': trainer,
                  'optimizer': optimizer}

        config_list = [{
            'sparsity': args.sparsity,
            'op_types': ["Linear"],
            'op_names': [x for layer in attention_name_groups[:6] for x in layer]
        },
            {
                'sparsity': args.sparsity / 2,
                'op_types': ["Linear"],
                'op_names': [x for layer in attention_name_groups[:6] for x in layer]
            }
        ]

    else:
        raise RuntimeError("Wrong usage number")

    pruner = TransformerHeadPruner(model, config_list, **kwargs)
    pruner.compress()

    #########################################################################
    # uncomment the following part to export the pruned model masks
    # model_path = os.path.join(args.output_dir, 'pruned_{}_{}.pth'.format(args.model_name_or_path, args.task_name))
    # mask_path = os.path.join(args.output_dir, 'mask_{}_{}.pth'.format(args.model_name_or_path, args.task_name))
    # pruner.export_model(model_path=model_path, mask_path=mask_path)

    #########################################################################
    # Speedup
    # Currently, speeding up Transformers through NNI ModelSpeedup is not supported because of shape inference issues.
    # However, if you are using the transformers library, you can use the following workaround:
    # The following code gets the head pruning decisions from the Pruner and calls the _prune_heads() function
    # implemented in models from the transformers library to speed up the model.
    if args.speed_up:
        speedup_rules = {}
        for group_idx, group in enumerate(pruner.attention_name_groups):
            # get the layer index
            layer_idx = None
            for part in group[0].split('.'):
                try:
                    layer_idx = int(part)
                    break
                except:
                    continue
            if layer_idx is not None:
                speedup_rules[layer_idx] = pruner.pruned_heads[group_idx]
        pruner._unwrap_model()
        model.bert._prune_heads(speedup_rules)
        print(model)

    #########################################################################
    # After pruning, finetune again on the target task
    # Get the metric function
    if args.task_name is not None:
        metric = load_metric("glue", args.task_name)
    else:
        metric = load_metric("accuracy")

    # re-initialize the optimizer and the scheduler
    model, optimizer, _, _, data_collator = get_dataloader_and_optimizer(args, tokenizer, model, train_dataset,
                                                                         eval_dataset)
    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps,
        num_training_steps=args.max_train_steps,
    )

    logger.info("***** Finetuning after Pruning *****")
    train_model(args, model, is_regression, train_dataloader, eval_dataloader, optimizer, lr_scheduler, metric, device)

    if args.output_dir is not None:
        torch.save(model.state_dict(), args.output_dir + '/model_after_pruning.pt')

    if args.task_name == "mnli":
        final_eval_for_mnli(args, model, processed_datasets, metric, data_collator)

    flops, params, results = count_flops_params(model, dummy_input)
    print(f'Final model FLOPs {flops / 1e6:.2f} M, #Params: {params / 1e6:.2f}M')


if __name__ == "__main__":
    main()
