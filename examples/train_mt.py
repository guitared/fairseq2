import enum
import functools
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

import torch
import torchtnt.framework as tnt
import torchtnt.utils

import fairseq2.callbacks
import fairseq2.dataloader.huggingface
import fairseq2.distributed
import fairseq2.nn
import fairseq2.optim.lr_scheduler
from fairseq2.generate import SpmTokenizer, spm_train
from fairseq2.nn import transformer
from fairseq2.tasks import TranslationTask

log = logging.getLogger(__name__)

BATCH_FIRST = True


class Mode(enum.Enum):
    TRAINING = 0
    EVALUATE = 1
    INFERENCE = 2
    TORCHHUB = 3


def train(
    task: TranslationTask,
    env: fairseq2.distributed.Env,
    lang_pairs: List[str],
    batch_size: int,
    reset: bool,
    eval_freq: int,
    wandb_project: str,
) -> None:
    task.__hubstate__.gen_hubconf(env.workdir)

    load_data = functools.partial(
        fairseq2.dataloader.huggingface.NllbDataLoader,
        tokenizer=task.tokenizer,
        batch_size=batch_size,
        env=env,
    )

    train = fairseq2.dataloader.RoundRobin(
        [load_data(*pair.split("-"), "train") for pair in lang_pairs],
        batch_first=BATCH_FIRST,
    )
    # Only evaluate on the first lang pair
    valid = load_data(*lang_pairs[0].split("-"), "valid")

    callbacks = fairseq2.callbacks.default_callbacks(
        task, env, wandb_project=wandb_project, reload_model=not reset
    )
    train_state = tnt.init_fit_state(train, valid, evaluate_every_n_steps=eval_freq)
    tnt.fit(train_state, task, callbacks=callbacks)


def evaluate(
    task: TranslationTask,
    env: fairseq2.distributed.Env,
    lang_pairs: List[str],
    batch_size: int,
) -> None:
    load_data = functools.partial(
        fairseq2.dataloader.huggingface.NllbDataLoader,
        tokenizer=task.tokenizer,
        batch_size=batch_size,
        env=env,
    )
    callbacks = fairseq2.callbacks.default_callbacks(task, env, reload_model=True)

    for lang_pair in lang_pairs:
        # Only evaluate on the first lang pair
        valid = load_data(*lang_pairs[0].split("-"), "valid")
        eval_state = tnt.init_fit_state([], valid)
        # eval_state = tnt.init_eval_state(dataloader=valid)
        log.info(f"Evaluating on {lang_pair} ...")
        tnt.evaluate(eval_state, task, callbacks=callbacks)


def inference(
    task: TranslationTask,
    env: fairseq2.distributed.Env,
    lang_pairs: List[str],
    batch_size: int,
    beam_search: str,
) -> None:
    import fairseq2.inference

    for lang_pair in lang_pairs:
        src, tgt = lang_pair.split("-")
        fairseq2.inference.inference(
            task,
            device=env.device,
            batch_size=batch_size,
            src_bos=src,
            tgt_bos=tgt,
            **json.loads(beam_search),
        )


DEFAULT_BEAM_SEARCH = json.dumps({"beam_size": 5, "max_len": 128, "unk_penalty": 1.0})


class MyBuilder(transformer.TransformerBuilder):
    pass


def main(
    workdir: Path,
    spm_path: Optional[Path] = None,
    langs: str = "cat_Latn-eng_Latn",
    small: bool = False,
    wandb_project: str = "nllb/fairseq2",
    batch_size: int = 16,
    partition: str = "debug",
    eval_freq: int = 10_000,
    num_gpus: int = 1,
    reset: bool = False,
    mode: Mode = Mode.TRAINING,
    beam_search: str = DEFAULT_BEAM_SEARCH,
) -> TranslationTask:
    workdir = Path(str(workdir).format(langs=langs))
    workdir.mkdir(exist_ok=True)
    # TODO: we should allow downloading the first time
    # os.environ["HF_DATASETS_OFFLINE"] = "1"
    # os.environ.update(os.environ)

    env = fairseq2.distributed.init(workdir, partition, num_gpus, one_file=True)
    torchtnt.utils.seed(0)
    torch.cuda.manual_seed(0)

    if spm_path is not None:
        assert spm_path.exists(), f"Spm not found: {spm_path}"
        (workdir / spm_path.name).symlink_to(spm_path)
    else:
        spm_path = workdir / "sentencepiece.model"

    if not spm_path.exists():
        spm_train_txt = workdir / "spm_train_combined.txt"
        # TODO handle more language pairs
        src, tgt = langs.split(",")[0].split("-", 1)
        cfg = spm_train.TrainSpmConfig(training_lines=1_000_000)
        fairseq2.dataloader.huggingface.NllbDataLoader.combine_and_dump(
            src,
            tgt,
            "train",
            spm_train_txt,
            limit=cfg.training_lines,
        )
        spm_train.train(cfg, spm_train_txt, spm_path)
        assert spm_path.exists()

    tokenizer = SpmTokenizer.from_file(spm_path, batch_first=BATCH_FIRST)
    lang_pairs = langs.split(",")
    src_langs = set(pair.split("-")[0] for pair in lang_pairs)
    tgt_langs = set(pair.split("-")[1] for pair in lang_pairs)
    lang_tokens = {}
    for lang in sorted(src_langs | tgt_langs):
        lang_tokens[lang] = tokenizer.add_special_token(lang)

    builder = MyBuilder(
        tokenizer.vocab_size(),
        tokenizer.PAD,
        batch_first=BATCH_FIRST,
        dropout_p=0,
        device=env.device,
    )

    task = TranslationTask(builder, tokenizer, env.device)
    if mode == Mode.TRAINING:
        train(task, env, lang_pairs, batch_size, reset, eval_freq, wandb_project)
    elif mode == Mode.EVALUATE:
        evaluate(task, env, lang_pairs, batch_size)
    elif mode == Mode.INFERENCE:
        inference(task, env, lang_pairs, batch_size, beam_search)
    elif mode == Mode.TORCHHUB:
        return task
    else:
        raise Exception(f"Unknown enum value: {mode}")

    sys.exit(0)


if __name__ == "__main__":
    import func_argparse

    func_argparse.single_main(main)