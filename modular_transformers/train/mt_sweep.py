import os

import torch
from torch.utils.data import DataLoader
from datasets import load_dataset, load_from_disk
import accelerate
from accelerate import Accelerator, DeepSpeedPlugin
from accelerate.utils import LoggerType, DummyOptim, DummyScheduler
from transformers import (
    AdamW,
    AutoTokenizer,
    AutoModelForCausalLM,
    get_linear_schedule_with_warmup,
    get_cosine_schedule_with_warmup,
    set_seed,
    AutoConfig,
)
from tqdm.auto import tqdm
import math
from modular_transformers.train.utils import Group_Texts
from pathlib import Path
import sys

from modular_transformers.models.gpt2.utils import initialize_gpt2_weights
import pickle

from modular_transformers.models.gpt2.configuration_gpt2 import GPT2Config
from modular_transformers.models import components

import wandb

import random

"""
Basic script to train a distill-gpt2 model using accelerate and grouping function.
Set config to use DeepSpeed
'accelerate config' -> enter in desired DeepSpeed configs or input path to deepspeed_config.json
'accelerate launch bplm/basic_accelerate_addedAug2022.py'
"""

MAX_GPU_BATCH_SIZE = 32
EVAL_BATCH_SIZE = 32
CONTEXT_LENGTH = 1024


# obviously hardcoding gradient accumulation steps is not ideal, but it's the only way to get the code to run
accelerator = Accelerator(
    log_with="wandb", gradient_accumulation_steps=64 // MAX_GPU_BATCH_SIZE
)


# Evaluate function
def evaluate(model, eval_dataloader, accelerator):
    model.eval()
    losses = []
    for step, batch in tqdm(enumerate(eval_dataloader), total=len(eval_dataloader)):
        with torch.no_grad():
            batch = torch.stack(batch["input_ids"]).transpose(1, 0)
            outputs = model(batch, labels=batch)
        losses.append(accelerator.gather(outputs.loss))
    loss = torch.mean(torch.stack(losses))
    try:
        perplexity = torch.exp(loss)
    except OverflowError:
        perplexity = float("inf")
    accelerator.print(
        f"validation loss: {loss.item()}, validation perplexity {perplexity.item()}"
    )
    return loss.item(), perplexity.item()


def main():
    data = "100M"
    batch_size = 64

    # Set training config --------------------------------------------

    train_config = {
        "lr": 0.0006,
        "num_epochs": 17,
        "correct_bias": True,
        "batch_size": batch_size,
        "data": data,
    }
    tokenizer = AutoTokenizer.from_pretrained("gpt2", fast=False)
    tokenizer.pad_token = tokenizer.eos_token

    path = "/om/weka/evlab/ehoseini/MyData/miniBERTa_v2/"
    grouped_pad_train = load_from_disk(
        os.path.join(
            path, f"miniBERTa-{data}-crunched", f"train_context_len_{CONTEXT_LENGTH}"
        )
    )
    grouped_pad_valid = load_from_disk(
        os.path.join(
            path, f"miniBERTa-{data}-crunched", f"valid_context_len_{CONTEXT_LENGTH}"
        )
    )

    if train_config["batch_size"] > MAX_GPU_BATCH_SIZE:
        gradient_accumulation_steps = train_config["batch_size"] // MAX_GPU_BATCH_SIZE
        batch_size = MAX_GPU_BATCH_SIZE

    eval_dataloader = DataLoader(
        grouped_pad_valid, shuffle=False, batch_size=EVAL_BATCH_SIZE
    )
    train_dataloader = DataLoader(
        grouped_pad_train, shuffle=True, batch_size=batch_size
    )
    del grouped_pad_train, grouped_pad_valid

    accelerator.init_trackers("bottleneck_sweep")
    tracker = accelerator.get_tracker("wandb", unwrap=True)
    bottleneck_dim = tracker.config["bottleneck"]
    n_layer = tracker.config["n_layer"]

    seed = random.randint(1, 10000)
    set_seed(seed)
    train_config["seed"] = seed

    config = {
        "regsize": bottleneck_dim,
        "vocab_size": len(tokenizer),
        "n_ctx": CONTEXT_LENGTH,
        "bos_token_id": tokenizer.bos_token_id,
        "eos_token_id": tokenizer.eos_token_id,
        "bottleneck": bottleneck_dim,
        "n_layer": n_layer,
    }
    config = GPT2Config(config)
    model = components.LM(config)

    run_name = "reg_loss"
    model_name = ""
    for layer in config.n_embds:
        model_name += f"{layer}-"
    model_name = model_name[:-1]

    # Logging initialization
    # Change name test to log to different project
    train_config.update(config.get())
    train_config.update({"wandb": {"name": model_name}})

    tracker.config.update(train_config)

    torch.cuda.empty_cache()
    model.output_loss = True
    model_size = sum(t.numel() for t in model.parameters())
    print(f"{model_name} size: {model_size / 1000 ** 2:.1f}M parameters")
    print(model)
    model = model.to(accelerator.device)

    # Define optimizer
    # Creates Dummy Optimizer if `optimizer` was specified in the config file else creates AdamW Optimizer
    optimizer_cls = (
        torch.optim.AdamW
        if accelerator.state.deepspeed_plugin is None
        or "optimizer" not in accelerator.state.deepspeed_plugin.deepspeed_config
        else DummyOptim
    )
    optimizer = optimizer_cls(params=model.parameters(), lr=train_config["lr"])
    if (
        accelerator.state.deepspeed_plugin is None
        or "scheduler" not in accelerator.state.deepspeed_plugin.deepspeed_config
    ):
        lr_scheduler = get_cosine_schedule_with_warmup(
            optimizer=optimizer,
            num_warmup_steps=100,
            num_training_steps=(
                len(train_dataloader) * 25
            ),  # usually 25 should be num_epochs but we're cutting of early here
        )
    else:
        assert False

    # Pass everything to accelerator
    model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = (
        accelerator.prepare(
            model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
        )
    )

    # Logging variables
    n_steps_per_epoch = math.ceil(len(train_dataloader.dataset) / batch_size)
    data_count = 0
    absolute_step = 0
    # Begin training for number of epochs

    for epoch in tqdm(range(train_config["num_epochs"])):
        model.train()
        torch.cuda.empty_cache()
        for step, batch in tqdm(
            enumerate(train_dataloader), total=len(train_dataloader)
        ):
            batch = [
                torch.stack(batch[x]).transpose(1, 0)
                for x in ["input_ids", "attention_mask"]
            ]

            with accelerator.accumulate(model):
                outputs = model(batch[0], labels=batch[0], attention_mask=batch[1])
                loss = outputs.loss

                accelerator.backward(loss)
                lr_scheduler.step()
                optimizer.step()
                optimizer.zero_grad()

            data_count += batch[0].shape[0]

            absolute_step += 1

            accelerator.log({"train/train_loss": loss}, step=absolute_step)
            accelerator.log(
                {"train/epoch": (absolute_step + 1) / n_steps_per_epoch},
                step=absolute_step,
            )
            accelerator.log({"train/data_count": data_count}, step=absolute_step)
            accelerator.log(
                {"train/learning_rate": lr_scheduler.get_last_lr()[0]},
                step=absolute_step,
            )

            if absolute_step % 200 == 0:
                model.eval()
                with torch.no_grad():
                    valid_loss, valid_accuracy = evaluate(
                        model, eval_dataloader, accelerator
                    )
                accelerator.log(
                    {"validation/valid_loss": valid_loss}, step=absolute_step
                )
                accelerator.log(
                    {"validation/valid_accuracy": valid_accuracy}, step=absolute_step
                )
                model.train()
                torch.cuda.empty_cache()

            if absolute_step % 20000 == 0:
                save_dir = Path(
                    f"/om2/user/jackking/MyData/mt/miniberta_{data}/{model_name}/{run_name}/checkpoint_{absolute_step}"
                )
                save_dir.mkdir(parents=True, exist_ok=True)
                accelerator.wait_for_everyone()
                unwrapped_model = accelerator.unwrap_model(model)
                unwrapped_model.save_pretrained(
                    save_dir,
                    is_main_process=accelerator.is_main_process,
                    save_function=accelerator.save,
                    state_dict=accelerator.get_state_dict(model),
                )
                accelerator.save(
                    {
                        "epoch": epoch,
                        "steps": step,
                        "optimizer": optimizer.state_dict(),
                        "scheduler": lr_scheduler.state_dict(),
                    },
                    os.path.join(save_dir, "accelerator_states"),
                )

    # Save final model
    save_dir = Path(
        f"/om2/user/jackking/MyData/mt/miniberta_{data}/{model_name}/{run_name}/checkpoint_{absolute_step}"
    )
    save_dir.mkdir(parents=True, exist_ok=True)
    accelerator.wait_for_everyone()
    unwrapped_model = accelerator.unwrap_model(model)
    unwrapped_model.save_pretrained(
        save_dir,
        is_main_process=accelerator.is_main_process,
        save_function=accelerator.save,
        state_dict=accelerator.get_state_dict(model),
    )
    accelerator.save(
        {
            "epoch": epoch,
            "steps": step,
            "optimizer": optimizer.state_dict(),
            "scheduler": lr_scheduler.state_dict(),
        },
        os.path.join(save_dir, "accelerator_states"),
    )

    accelerator.end_training()
    torch.cuda.empty_cache()


if __name__ == "__main__":

    wandb.login(key="a338f755915cccd861b14f29bf68601d8e1ec2c9")

    sweep_configuration = {
        "method": "grid",
        "name": "bottleneck_sweep",
        "metric": {"goal": "minimize", "name": "validation/valid_loss"},
        "parameters": {
            # 'n_layer': {'values': [3, 4, 5, 6, 7, 8]},
            "n_layer": {"values": [5, 7]},
            # 'bottleneck': {'values': [256, 384, 576, 768, 960, 1152, 128]},
            "bottleneck": {"values": [788]},
            "random_seed_num": {"values": [3, 4]},
        },
    }

    # sweep_id = wandb.sweep(sweep_configuration, project="bottleneck_sweep")
    # print(sweep_id)
    # sweep_id = "bwavwwqk"
    # wandb.agent(sweep_id=sweep_id, project="bottleneck_sweep", function=main)

    # sweep_id = "wpdanl57"
    # wandb.agent(sweep_id=sweep_id, project="bottleneck_sweep", function=main)

    sweep_id = "47svg0eu"
    wandb.agent(sweep_id=sweep_id, project="bottleneck_sweep", function=main)
