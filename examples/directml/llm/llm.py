# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import argparse
import json
import os
import shutil
import warnings
from pathlib import Path
from typing import Optional

import config
import torch
import transformers
from chat_app.app import launch_chat_app
from huggingface_hub import hf_hub_download
from run_llm_io_binding import run_llm_io_binding

from olive.model import ONNXModelHandler
from olive.workflows import run as olive_run


def set_config_parameters(repo_id: str, num_layers: Optional[int]):
    tokenizer = transformers.AutoTokenizer.from_pretrained(repo_id)

    pipeline = transformers.pipeline(
        "text-generation", model=repo_id, tokenizer=tokenizer, torch_dtype=torch.float32, device="cpu"
    )

    config.hidden_size = pipeline.model.config.hidden_size
    config.intermediate_size = pipeline.model.config.intermediate_size
    config.num_heads = pipeline.model.config.num_attention_heads
    config.num_key_value_heads = pipeline.model.config.num_key_value_heads
    config.num_layers = num_layers or pipeline.model.config.num_hidden_layers
    config.vocab_size = pipeline.model.config.vocab_size

    if hasattr(pipeline.model.config, "rms_norm_eps"):
        config.normalization_type = "rms"
        config.epsilon = pipeline.model.config.rms_norm_eps
    elif hasattr(pipeline.model.config, "layer_norm_epsilon"):
        config.normalization_type = "layer_norm"
        config.epsilon = pipeline.model.config.layer_norm_epsilon
    else:
        raise ValueError("Normalization epsilon value was not found")

    config.normalization_type = "rms" if hasattr(pipeline.model.config, "rms_norm_eps") else "layer_norm"
    config.strict_weights_loading = config.num_layers == pipeline.model.config.num_hidden_layers
    config.state_dict = pipeline.model.state_dict()


def optimize(optimized_model_dir: Path, repo_id: str, model_name: str, num_layers: Optional[int]):
    print(f"\nOptimizing {repo_id}")

    set_config_parameters(repo_id, num_layers)

    script_dir = Path(__file__).resolve().parent
    model_info = {}

    with Path.open(script_dir / "config_llm.json") as fin:
        olive_config = json.load(fin)
        olive_config["engine"]["output_name"] = model_name
        olive_config["passes"]["optimize"]["config"]["hidden_size"] = config.hidden_size
        olive_config["passes"]["optimize"]["config"]["num_heads"] = config.num_heads
        olive_config["passes"]["optimize"]["config"]["num_key_value_heads"] = config.num_key_value_heads

        # Fewer than 32 layers can be provided for debugging purposes so we have to remove them from the config
        if config.num_layers < 32:
            model_components = olive_config["input_model"]["config"]["model_components"]
            for model_component in model_components:
                layer_range = range(config.num_layers, 32)

                # Remove the extra inputs
                key_inputs_to_remove = {f"cache.{idx}.key" for idx in layer_range}
                value_inputs_to_remove = {f"cache.{idx}.value" for idx in layer_range}
                input_names = model_component["config"]["io_config"]["input_names"]
                input_names = [x for x in input_names if x not in key_inputs_to_remove]
                input_names = [x for x in input_names if x not in value_inputs_to_remove]
                model_component["config"]["io_config"]["input_names"] = input_names

                # Remove the extra outputs
                key_output_to_remove = {f"cache_out.{idx}.key" for idx in layer_range}
                value_output_to_remove = {f"cache_out.{idx}.value" for idx in layer_range}
                output_names = model_component["config"]["io_config"]["output_names"]
                output_names = [x for x in output_names if x not in key_output_to_remove]
                output_names = [x for x in output_names if x not in value_output_to_remove]
                model_component["config"]["io_config"]["output_names"] = output_names

                # Remove the dynamic axes
                for idx in layer_range:
                    del model_component["config"]["io_config"]["dynamic_axes"][f"cache.{idx}.key"]
                    del model_component["config"]["io_config"]["dynamic_axes"][f"cache.{idx}.value"]

        olive_run(olive_config)

        footprints_file_path = Path(__file__).resolve().parent / "footprints" / f"{model_name}_gpu-dml_footprints.json"
        with footprints_file_path.open("r") as footprint_file:
            footprints = json.load(footprint_file)

            conversion_footprint = None
            optimizer_footprint = None
            merging_footprint = None
            for footprint in footprints.values():
                if footprint["from_pass"] == "OnnxConversion":
                    conversion_footprint = footprint
                elif footprint["from_pass"] == "OrtTransformersOptimization":
                    optimizer_footprint = footprint
                elif footprint["from_pass"] == "OptimumMerging":
                    merging_footprint = footprint

            assert conversion_footprint is not None
            assert optimizer_footprint is not None
            assert merging_footprint is not None
            optimized_olive_model = ONNXModelHandler(**merging_footprint["model_config"]["config"])

            model_info[model_name] = {
                "optimized": {
                    "path": Path(optimized_olive_model.model_path),
                },
            }

            print(f"Optimized Model   : {model_info[model_name]['optimized']['path']}")

    print("Copying optimized model...")

    # Copy the ONNX models
    src_path = model_info[model_name]["optimized"]["path"]
    dst_path = optimized_model_dir / model_name / src_path.name
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    shutil.copyfile(src_path, dst_path)

    # Copy the weights
    src_weights_path = src_path.with_suffix(".onnx.data")
    if src_weights_path.is_file():
        dst_weights_path = dst_path.with_suffix(".onnx.data")
        shutil.copyfile(src_weights_path, dst_weights_path)

    # Copy the tokenizer file
    src_tokenizer_path = hf_hub_download(repo_id=repo_id, filename="tokenizer.json")
    dst_tokenizer_path = dst_path.parents[0] / "tokenizer.json"
    shutil.copyfile(src_tokenizer_path, dst_tokenizer_path)

    # Copy the tokenizer config file
    src_tokenizer_config_path = hf_hub_download(repo_id=repo_id, filename="tokenizer_config.json")
    dst_tokenizer_config_path = dst_path.parents[0] / "tokenizer_config.json"
    shutil.copyfile(src_tokenizer_config_path, dst_tokenizer_config_path)

    print(f"The optimized pipeline is located here: {optimized_model_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimize", action="store_true", help="Runs the optimization step")
    parser.add_argument("--interactive", action="store_true", help="Run with a GUI")
    parser.add_argument(
        "--expose_locally",
        action="store_true",
        help="Expose the web UI on the local network (does nothing if --interactive is not supplied)",
    )
    parser.add_argument("--prompt", default="What is the lightest element?", type=str)
    parser.add_argument("--max_seq_len", default=2048, type=int, help="The size of the cache")
    parser.add_argument("--device_id", default=0, type=int, help="GPU device to use during inference")
    parser.add_argument(
        "--max_gen_len", default=256, type=int, help="The maximum number of tokens that can be included in an answer"
    )
    parser.add_argument("--device", type=str, choices=["dml", "cuda"], default="dml")
    parser.add_argument(
        "--model_type",
        default="llama-2-7b-chat",
        choices=["llama-2-7b-chat", "mistral-7b-chat"],
        help="Which model to convert.",
        type=str,
    )
    parser.add_argument(
        "--num_layers",
        help="This is a debugging option to be able to quickly generate and optimize an ONNX model with fewer layers "
        "that barely takes any memory and is easy to load in Netron. This value should NOT be provided for production "
        "purposes.",
        type=int,
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    optimized_model_dir = script_dir / "models" / "optimized"

    repo_id = {
        "llama-2-7b-chat": "meta-llama/Llama-2-7b-chat-hf",
        "mistral-7b-chat": "mistralai/Mistral-7B-Instruct-v0.1",
    }[args.model_type]

    model_name = repo_id.replace("/", "_")

    if args.optimize or not (optimized_model_dir / model_name).exists():
        optimize(optimized_model_dir, repo_id, model_name, args.num_layers)

    if not args.optimize:
        if args.interactive:
            launch_chat_app(args.expose_locally)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                run_llm_io_binding(
                    optimized_model_dir / model_name,
                    args.prompt,
                    args.max_seq_len,
                    args.max_gen_len,
                    args.device,
                    args.device_id,
                )
