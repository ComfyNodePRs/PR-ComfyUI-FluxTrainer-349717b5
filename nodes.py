import os
import torch
from torchvision import transforms

import folder_paths
import comfy.model_management as mm
import comfy.utils
import toml
import json
import time
import shutil
from pathlib import Path
script_directory = os.path.dirname(os.path.abspath(__file__))

from .flux_train_network_comfy import FluxNetworkTrainer
from .library import flux_train_utils as  flux_train_utils
from .flux_train_comfy import FluxTrainer
from .flux_train_comfy import setup_parser as train_setup_parser
from .library.device_utils import init_ipex
init_ipex()

from .library import train_util
from .train_network import setup_parser as train_network_setup_parser
import matplotlib.pyplot as plt
import io
from PIL import Image

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class FluxTrainModelSelect:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "transformer": (folder_paths.get_filename_list("unet"), ),
            "vae": (folder_paths.get_filename_list("vae"), ),
            "clip_l": (folder_paths.get_filename_list("clip"), ),
            "t5": (folder_paths.get_filename_list("clip"), ),
           },
        }

    RETURN_TYPES = ("TRAIN_FLUX_MODELS",)
    RETURN_NAMES = ("flux_models",)
    FUNCTION = "loadmodel"
    CATEGORY = "FluxTrainer"

    def loadmodel(self, transformer, vae, clip_l, t5):
        
        transformer_path = folder_paths.get_full_path("unet", transformer)
        vae_path = folder_paths.get_full_path("vae", vae)
        clip_path = folder_paths.get_full_path("clip", clip_l)
        t5_path = folder_paths.get_full_path("clip", t5)

        flux_models = {
            "transformer": transformer_path,
            "vae": vae_path,
            "clip_l": clip_path,
            "t5": t5_path
        }
        
        return (flux_models,)

class TrainDatasetGeneralConfig:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "color_aug": ("BOOLEAN",{"default": False, "tooltip": "enable weak color augmentation"}),
            "flip_aug": ("BOOLEAN",{"default": False, "tooltip": "enable horizontal flip augmentation"}),
            "shuffle_caption": ("BOOLEAN",{"default": False, "tooltip": "shuffle caption"}),
            "caption_dropout_rate": ("FLOAT",{"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01,"tooltip": "tag dropout rate"}),
            },
        }

    RETURN_TYPES = ("JSON",)
    RETURN_NAMES = ("dataset_general",)
    FUNCTION = "create_config"
    CATEGORY = "FluxTrainer"

    def create_config(self, shuffle_caption, caption_dropout_rate, color_aug, flip_aug):
        
        dataset = {
           "general": {
                "shuffle_caption": shuffle_caption,
                "caption_extension": ".txt",
                "keep_tokens_separator": "|||",
                "caption_dropout_rate": caption_dropout_rate,
                "color_aug": color_aug,
                "flip_aug": flip_aug,
           },
           "datasets": []
        }
        dataset_json = json.dumps(dataset, indent=2)
        print(dataset_json)
        return (dataset_json,)

class TrainDatasetAdd:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "dataset_config": ("JSON",),
            "width": ("INT",{"min": 64, "default": 1024, "tooltip": "image width when bucketing is not used, also the default validation sampling width"}),
            "height": ("INT",{"min": 64, "default": 1024, "tooltip": "image height when bucketing is not used, also the default validation sampling height"}),
            "batch_size": ("INT",{"min": 1, "default": 2, "tooltip": "Higher batch size uses more memory and generalizes the training more. "}),
            "dataset_path": ("STRING",{"multiline": True, "default": "", "tooltip": "path to dataset, root is ComfyUI folder"}),
            "class_tokens": ("STRING",{"multiline": True, "default": "", "tooltip": "class token, aka trigger word"}),
            "enable_bucket": ("BOOLEAN",{"default": True, "tooltip": "enable buckets for multi aspect ratio training"}),
            "bucket_no_upscale": ("BOOLEAN",{"default": False, "tooltip": "bucket reso is defined by image size automatically"}),
            "num_repeats": ("INT", {"default": 1, "min": 1, "tooltip": "number of times to repeat dataset for an epoch"}),
            },
        }

    RETURN_TYPES = ("JSON",)
    RETURN_NAMES = ("dataset",)
    FUNCTION = "create_config"
    CATEGORY = "FluxTrainer"

    def create_config(self, dataset_config, dataset_path, class_tokens, width, height, batch_size, num_repeats, enable_bucket,  
                  bucket_no_upscale):
        
        dataset = {
           "datasets": [
               {
                   "resolution": (width, height),
                   "batch_size": batch_size,
                   "enable_bucket": enable_bucket,
                   "bucket_no_upscale": bucket_no_upscale,
                  
                   "subsets": [
                       {
                           "image_dir": dataset_path,
                           "class_tokens": class_tokens,
                           "num_repeats": num_repeats
                       }
                   ]
               }
           ]
        }
        
        updated_dataset = json.loads(dataset_config)
        updated_dataset["datasets"].extend(dataset["datasets"])
        
        updated_dataset_json = json.dumps(updated_dataset, indent=2)
        print(updated_dataset_json)
        return (updated_dataset_json,)

class OptimizerConfig:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "optimizer_type": (["adamw8bit", "adamw","prodigy", "CAME"], {"default": "adamw8bit", "tooltip": "optimizer type"}),
            "max_grad_norm": ("FLOAT",{"default": 1.0, "min": 0.0, "tooltip": "gradient clipping"}),
            "lr_scheduler": (["constant", "cosine", "cosine_with_restarts", "polynomial", "constant_with_warmup"], {"default": "constant", "tooltip": "learning rate scheduler"}),
            "lr_warmup_steps": ("INT",{"default": 0, "min": 0, "tooltip": "learning rate warmup steps"}),
            "lr_scheduler_num_cycles": ("INT",{"default": 1, "min": 1, "tooltip": "learning rate scheduler num cycles"}),
            "lr_scheduler_power": ("FLOAT",{"default": 1.0, "min": 0.0, "tooltip": "learning rate scheduler power"}),
            "min_snr_gamma": ("FLOAT",{"default": 5.0, "min": 0.0, "tooltip": "min snr gamma"}),
           },
        }

    RETURN_TYPES = ("ARGS",)
    RETURN_NAMES = ("optimizer_settings",)
    FUNCTION = "create_config"
    CATEGORY = "FluxTrainer"

    def create_config(self, min_snr_gamma, **kwargs):
        kwargs["min_snr_gamma"] = min_snr_gamma if min_snr_gamma != 0.0 else None
        return (kwargs,)

class OptimizerConfigAdafactor:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "max_grad_norm": ("FLOAT",{"default": 1.0, "min": 0.0, "tooltip": "gradient clipping"}),
            "lr_scheduler": (["constant", "cosine", "cosine_with_restarts", "polynomial", "constant_with_warmup", "adafactor"], {"default": "constant", "tooltip": "learning rate scheduler"}),
            "lr_warmup_steps": ("INT",{"default": 0, "min": 0, "tooltip": "learning rate warmup steps"}),
            "lr_scheduler_num_cycles": ("INT",{"default": 1, "min": 1, "tooltip": "learning rate scheduler num cycles"}),
            "lr_scheduler_power": ("FLOAT",{"default": 1.0, "min": 0.0, "tooltip": "learning rate scheduler power"}),
            "relative_step": ("BOOLEAN",{"default": False, "tooltip": "relative step"}),
            "scale_parameter": ("BOOLEAN",{"default": False, "tooltip": "scale parameter"}),
            "warmup_init": ("BOOLEAN",{"default": False, "tooltip": "warmup init"}),
            "clip_threshold": ("FLOAT",{"default": 1.0, "min": 0.0, "tooltip": "clip threshold"}),
           },
        }

    RETURN_TYPES = ("ARGS",)
    RETURN_NAMES = ("optimizer_settings",)
    FUNCTION = "create_config"
    CATEGORY = "FluxTrainer"

    def create_config(self, relative_step, scale_parameter, warmup_init, clip_threshold, **kwargs):
        kwargs["optimizer_type"] = "adafactor"
        kwargs["optimizer_args"] = [
                f"relative_step={relative_step}",
                f"scale_parameter={scale_parameter}",
                f"warmup_init={warmup_init}",
                f"clip_threshold={clip_threshold}"
            ]
        return (kwargs,)    

class InitFluxLoRATraining:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "flux_models": ("TRAIN_FLUX_MODELS",),
            "dataset": ("JSON",),
            "optimizer_settings": ("ARGS",),
            "output_name": ("STRING", {"default": "flux_lora", "multiline": False}),
            "output_dir": ("STRING", {"default": "flux_trainer_output", "multiline": False}),
            "network_dim": ("INT", {"default": 4, "min": 1, "max": 256, "step": 1, "tooltip": "network dim"}),
            "network_alpha": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 256.0, "step": 0.01, "tooltip": "network alpha"}),
            "learning_rate": ("FLOAT", {"default": 4e-4, "min": 0.0, "max": 10.0, "step": 0.00001, "tooltip": "learning rate"}),
            #"unet_lr": ("FLOAT", {"default": 1e-4, "min": 0.0, "max": 10.0, "step": 0.00001, "tooltip": "unet learning rate"}),
            #"max_train_epochs": ("INT", {"default": 4, "min": 1, "max": 1000, "step": 1, "tooltip": "max number of training epochs"}),
            "max_train_steps": ("INT", {"default": 1500, "min": 1, "max": 10000, "step": 1, "tooltip": "max number of training steps"}),
            #"network_train_unet_only": ("BOOLEAN", {"default": True, "tooltip": "wheter to train the text encoder"}),
            #"text_encoder_lr": ("FLOAT", {"default": 0, "min": 0.0, "max": 10.0, "step": 0.00001, "tooltip": "text encoder learning rate"}),
            "apply_t5_attn_mask": ("BOOLEAN", {"default": True, "tooltip": "apply t5 attention mask"}),
            #"t5xxl_max_token_length": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 8, "tooltip": "dev uses 512, schnell 256"}),
            "cache_latents": (["disk", "memory", "disabled"], {"tooltip": "caches text encoder outputs"}),
            "cache_text_encoder_outputs": (["disk", "memory", "disabled"], {"tooltip": "caches text encoder outputs"}),
            "split_mode": ("BOOLEAN", {"default": False, "tooltip": "[EXPERIMENTAL] use split mode for Flux model, network arg `train_blocks=single` is required"}),
            "weighting_scheme": (["logit_normal", "sigma_sqrt", "mode", "cosmap", "none"],),
            "logit_mean": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "mean to use when using the logit_normal weighting scheme"}),
            "logit_std": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,"tooltip": "std to use when using the logit_normal weighting scheme"}),
            "mode_scale": ("FLOAT", {"default": 1.29, "min": 0.0, "max": 10.0, "step": 0.01, "tooltip": "Scale of mode weighting scheme. Only effective when using the mode as the weighting_scheme"}),
            "timestep_sampling": (["sigmoid", "uniform", "sigma"], {"tooltip": "method to sample timestep"}),
            "sigmoid_scale": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.1, "tooltip": "Scale factor for sigmoid timestep sampling (only used when timestep-sampling is sigmoid"}),
            "model_prediction_type": (["raw", "additive", "sigma_scaled"], {"tooltip": "How to interpret and process the model prediction: raw (use as is), additive (add to noisy input), sigma_scaled (apply sigma scaling)."}),
            "guidance_scale": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 32.0, "step": 0.01, "tooltip": "guidance scale, for Flux training should be 1.0"}),
            "discrete_flow_shift": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01, "tooltip": "for the Euler Discrete Scheduler, default is 3.0"}),
            "highvram": ("BOOLEAN", {"default": False, "tooltip": "memory mode"}),
            "fp8_base": ("BOOLEAN", {"default": True, "tooltip": "use fp8 for base model"}),
            "gradient_dtype": (["fp32", "fp16", "bf16"], {"default": "fp32", "tooltip": "the actual dtype training uses"}),
            "save_dtype": (["fp32", "fp16", "bf16", "fp8_e4m3fn"], {"default": "bf16", "tooltip": "the dtype to save checkpoints as"}),
            "attention_mode": (["sdpa", "xformers", "disabled"], {"default": "sdpa", "tooltip": "memory efficient attention mode"}),
            "sample_prompts": ("STRING", {"multiline": True, "default": "illustration of a kitten | photograph of a turtle", "tooltip": "validation sample prompts, for multiple prompts, separate by `|`"}),
            },
        }

    RETURN_TYPES = ("NETWORKTRAINER", "INT", "KOHYA_ARGS",)
    RETURN_NAMES = ("network_trainer", "epochs_count", "args",)
    FUNCTION = "init_training"
    CATEGORY = "FluxTrainer"

    def init_training(self, flux_models, dataset, optimizer_settings, sample_prompts, output_name, attention_mode, 
                      gradient_dtype, save_dtype, **kwargs,):
        mm.soft_empty_cache()

        output_dir = os.path.abspath(kwargs.get("output_dir"))
    
        total, used, free = shutil.disk_usage(output_dir)
 
        required_free_space = 2 * (2**30)
        if free <= required_free_space:
            raise ValueError(f"Insufficient disk space. Required: {required_free_space/2**30}GB. Available: {free/2**30}GB")
        
        dataset_toml = toml.dumps(json.loads(dataset))
        parser = train_network_setup_parser()
        args, _ = parser.parse_known_args()

        if kwargs.get("cache_latents") == "memory":
            kwargs["cache_latents"] = True
            kwargs["cache_latents_to_disk"] = False
        elif kwargs.get("cache_latents") == "disk":
            kwargs["cache_latents"] = True
            kwargs["cache_latents_to_disk"] = True
            kwargs["caption_dropout_rate"] = 0.0
            kwargs["shuffle_caption"] = False
            kwargs["token_warmup_step"] = 0.0
            kwargs["caption_tag_dropout_rate"] = 0.0
        else:
            kwargs["cache_latents"] = False
            kwargs["cache_latents_to_disk"] = False

        if kwargs.get("cache_text_encoder_outputs") == "memory":
            kwargs["cache_text_encoder_outputs"] = True
            kwargs["cache_text_encoder_outputs_to_disk"] = False
        elif kwargs.get("cache_text_encoder_outputs") == "disk":
            kwargs["cache_text_encoder_outputs"] = True
            kwargs["cache_text_encoder_outputs_to_disk"] = True
        else:
            kwargs["cache_text_encoder_outputs"] = False
            kwargs["cache_text_encoder_outputs_to_disk"] = False

        if '|' in sample_prompts:
            prompts = sample_prompts.split('|')
        else:
            prompts = [sample_prompts]

        config_dict = {
            "sample_prompts": prompts,
            "save_precision": save_dtype,
            "mixed_precision": "bf16",
            "num_cpu_threads_per_process": 1,
            "pretrained_model_name_or_path": flux_models["transformer"],
            "clip_l": flux_models["clip_l"],
            "t5xxl": flux_models["t5"],
            "ae": flux_models["vae"],
            "save_model_as": "safetensors",
            "persistent_data_loader_workers": False,
            "max_data_loader_n_workers": 0,
            "seed": 42,
            "gradient_checkpointing": True,
            "network_module": "networks.lora_flux",
            "dataset_config": dataset_toml,
            "output_name": f"{output_name}_rank{kwargs.get('network_dim')}_{save_dtype}",
            "loss_type": "l2",
            "width" : 1024,
            "height" : 1024,
            "text_encoder_lr": 0,
            "network_train_unet_only": True,
            "t5xxl_max_token_length": 512,
        }
        attention_settings = {
            "sdpa": {"mem_eff_attn": True, "xformers": False, "spda": True},
            "xformers": {"mem_eff_attn": True, "xformers": True, "spda": False}
        }
        config_dict.update(attention_settings.get(attention_mode, {}))

        gradient_dtype_settings = {
            "fp16": {"full_fp16": True, "full_bf16": False, "mixed_precision": "fp16"},
            "bf16": {"full_bf16": True, "full_fp16": False, "mixed_precision": "bf16"}
        }
        config_dict.update(gradient_dtype_settings.get(gradient_dtype, {}))

        config_dict.update(kwargs)
        config_dict.update(optimizer_settings)

        for key, value in config_dict.items():
            setattr(args, key, value)

        with torch.inference_mode(False):
            network_trainer = FluxNetworkTrainer()
            training_loop = network_trainer.init_train(args)

        epochs_count = network_trainer.num_train_epochs

        os.makedirs(output_dir, exist_ok=True)
        saved_args_file_path = os.path.join(output_dir, f"{output_name}_args.json")
        with open(saved_args_file_path, 'w') as f:
            json.dump(vars(args), f, indent=4)

        trainer = {
            "network_trainer": network_trainer,
            "training_loop": training_loop,
        }
        return (trainer, epochs_count, args)

class InitFluxTraining:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "flux_models": ("TRAIN_FLUX_MODELS",),
            "dataset": ("JSON",),
            "optimizer_settings": ("ARGS",),
            "output_name": ("STRING", {"default": "flux", "multiline": False}),
            "output_dir": ("STRING", {"default": "flux_trainer_output", "multiline": False, "tooltip": "output directory, root is ComfyUI folder"}),
            "learning_rate": ("FLOAT", {"default": 5e-5, "min": 0.0, "max": 10.0, "step": 0.00001, "tooltip": "learning rate"}),
            "max_train_steps": ("INT", {"default": 1500, "min": 1, "max": 10000, "step": 1, "tooltip": "max number of training steps"}),
            "apply_t5_attn_mask": ("BOOLEAN", {"default": True, "tooltip": "apply t5 attention mask"}),
            "t5xxl_max_token_length": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 8, "tooltip": "dev uses 512, schnell 256"}),
            "cache_latents": (["disk", "memory", "disabled"], {"tooltip": "caches text encoder outputs"}),
            "cache_text_encoder_outputs": (["disk", "memory", "disabled"], {"tooltip": "caches text encoder outputs"}),
            "weighting_scheme": (["logit_normal", "sigma_sqrt", "mode", "cosmap", "none"],),
            "logit_mean": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "mean to use when using the logit_normal weighting scheme"}),
            "logit_std": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,"tooltip": "std to use when using the logit_normal weighting scheme"}),
            "mode_scale": ("FLOAT", {"default": 1.29, "min": 0.0, "max": 10.0, "step": 0.01, "tooltip": "Scale of mode weighting scheme. Only effective when using the mode as the weighting_scheme"}),
            "loss_type": (["l1", "l2", "huber", "smooth_l1"], {"default": "l2", "tooltip": "loss type"}),
            "timestep_sampling": (["sigmoid", "uniform", "sigma"], {"tooltip": "method to sample timestep"}),
            "sigmoid_scale": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.1, "tooltip": "Scale factor for sigmoid timestep sampling (only used when timestep-sampling is sigmoid"}),
            "model_prediction_type": (["raw", "additive", "sigma_scaled"], {"tooltip": "How to interpret and process the model prediction: raw (use as is), additive (add to noisy input), sigma_scaled (apply sigma scaling)."}),
            "cpu_offload_checkpointing": ("BOOLEAN", {"default": True, "tooltip": "offload the gradient checkpointing to CPU. This reduces VRAM usage for about 2GB"}),
            "blockwise_fused_optimizers": ("BOOLEAN", {"default": True, "tooltip": "enables the fusing of the optimizer for each block"}),
            "single_blocks_to_swap": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1, "tooltip": "number of single blocks to swap. The default is 0. This option must be combined with blockwise_fused_optimizers"}),
            "double_blocks_to_swap": ("INT", {"default": 6, "min": 0, "max": 100, "step": 1, "tooltip": "number of double blocks to swap. This option must be combined with blockwise_fused_optimizers"}),
            "guidance_scale": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 32.0, "step": 0.01, "tooltip": "guidance scale"}),
            "discrete_flow_shift": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01, "tooltip": "for the Euler Discrete Scheduler, default is 3.0"}),
            "highvram": ("BOOLEAN", {"default": False, "tooltip": "memory mode"}),
            "fp8_base": ("BOOLEAN", {"default": False, "tooltip": "use fp8 for base model"}),
            "gradient_dtype": (["fp32", "fp16", "bf16"], {"default": "fp32", "tooltip": "to use the full fp16/bf16 training"}),
            "save_dtype": (["fp32", "fp16", "bf16", "fp8_e4m3fn"], {"default": "bf16", "tooltip": "the dtype to save checkpoints as"}),
            "attention_mode": (["sdpa", "xformers", "disabled"], {"default": "sdpa", "tooltip": "memory efficient attention mode"}),
            "sample_prompts": ("STRING", {"multiline": True, "default": "illustration of a kitten | photograph of a turtle", "tooltip": "validation sample prompts, for multiple prompts, separate by `|`"}),
            },
        }

    RETURN_TYPES = ("NETWORKTRAINER", "INT", "KOHYA_ARGS")
    RETURN_NAMES = ("network_trainer", "epochs_count", "args")
    FUNCTION = "init_training"
    CATEGORY = "FluxTrainer"

    def init_training(self, flux_models, optimizer_settings, dataset, sample_prompts, output_name, 
                      attention_mode, gradient_dtype, save_dtype, **kwargs,):
        mm.soft_empty_cache()

        output_dir = os.path.abspath(kwargs.get("output_dir"))
    
        total, used, free = shutil.disk_usage(output_dir)
        required_free_space = 25 * (2**30)
        if free <= required_free_space:
            raise ValueError(f"Most likely insufficient disk space to complete training. Required: {required_free_space/2**30}GB. Available: {free/2**30}GB")

        dataset_toml = toml.dumps(json.loads(dataset))
        
        parser = train_setup_parser()
        args, _ = parser.parse_known_args()

        if kwargs.get("cache_latents") == "memory":
            kwargs["cache_latents"] = True
            kwargs["cache_latents_to_disk"] = False
        elif kwargs.get("cache_latents") == "disk":
            kwargs["cache_latents"] = True
            kwargs["cache_latents_to_disk"] = True
            kwargs["caption_dropout_rate"] = 0.0
            kwargs["shuffle_caption"] = False
            kwargs["token_warmup_step"] = 0.0
            kwargs["caption_tag_dropout_rate"] = 0.0
        else:
            kwargs["cache_latents"] = False
            kwargs["cache_latents_to_disk"] = False

        if kwargs.get("cache_text_encoder_outputs") == "memory":
            kwargs["cache_text_encoder_outputs"] = True
            kwargs["cache_text_encoder_outputs_to_disk"] = False
        elif kwargs.get("cache_text_encoder_outputs") == "disk":
            kwargs["cache_text_encoder_outputs"] = True
            kwargs["cache_text_encoder_outputs_to_disk"] = True
        else:
            kwargs["cache_text_encoder_outputs"] = False
            kwargs["cache_text_encoder_outputs_to_disk"] = False

        if '|' in sample_prompts:
            prompts = sample_prompts.split('|')
        else:
            prompts = [sample_prompts]

        config_dict = {
            "sample_prompts": prompts,
            "save_precision": save_dtype,
            "mixed_precision": "bf16",
            "num_cpu_threads_per_process": 1,
            "pretrained_model_name_or_path": flux_models["transformer"],
            "clip_l": flux_models["clip_l"],
            "t5xxl": flux_models["t5"],
            "ae": flux_models["vae"],
            "save_model_as": "safetensors",
            "persistent_data_loader_workers": False,
            "max_data_loader_n_workers": 0,
            "seed": 42,
            "gradient_checkpointing": True,
            "dataset_config": dataset_toml,
            "output_name": f"{output_name}_rank{kwargs.get('network_dim')}_{save_dtype}",
            "network_module": "networks.lora_flux",
            "width" : 1024,
            "height" : 1024,

        }
        attention_settings = {
            "sdpa": {"mem_eff_attn": True, "xformers": False, "spda": True},
            "xformers": {"mem_eff_attn": True, "xformers": True, "spda": False}
        }
        config_dict.update(attention_settings.get(attention_mode, {}))

        gradient_dtype_settings = {
            "fp16": {"full_fp16": True, "full_bf16": False, "mixed_precision": "fp16"},
            "bf16": {"full_bf16": True, "full_fp16": False, "mixed_precision": "bf16"}
        }
        config_dict.update(gradient_dtype_settings.get(gradient_dtype, {}))

        config_dict.update(kwargs)
        config_dict.update(optimizer_settings)

        for key, value in config_dict.items():
            setattr(args, key, value)

        with torch.inference_mode(False):
            network_trainer = FluxTrainer()
            training_loop = network_trainer.init_train(args)

        epochs_count = network_trainer.num_train_epochs

        os.makedirs(output_dir, exist_ok=True)
        saved_args_file_path = os.path.join(output_dir, f"{output_name}_args.json")
        with open(saved_args_file_path, 'w') as f:
            json.dump(vars(args), f, indent=4)

        trainer = {
            "network_trainer": network_trainer,
            "training_loop": training_loop,
        }
        return (trainer, epochs_count, args)

class InitFluxTrainingFromPreset:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "flux_models": ("TRAIN_FLUX_MODELS",),
            "dataset_settings": ("TOML_DATASET",),
            "preset_args": ("KOHYA_ARGS",),
            "output_name": ("STRING", {"default": "flux", "multiline": False}),
            "output_dir": ("STRING", {"default": "flux_trainer_output", "multiline": False, "tooltip": "output directory, root is ComfyUI folder"}),
            "sample_prompts": ("STRING", {"multiline": True, "default": "illustration of a kitten | photograph of a turtle", "tooltip": "validation sample prompts, for multiple prompts, separate by `|`"}),
            },
        }

    RETURN_TYPES = ("NETWORKTRAINER", "INT", "STRING", "KOHYA_ARGS")
    RETURN_NAMES = ("network_trainer", "epochs_count", "output_path", "args")
    FUNCTION = "init_training"
    CATEGORY = "FluxTrainer"

    def init_training(self, flux_models, dataset_settings, sample_prompts, output_name, preset_args, **kwargs,):
        mm.soft_empty_cache()

        dataset = dataset_settings["dataset"]
        dataset_repeats = dataset_settings["repeats"]
        
        parser = train_setup_parser()
        args, _ = parser.parse_known_args()
        for key, value in vars(preset_args).items():
            setattr(args, key, value)
        
        output_dir = os.path.join(script_directory, "output")
        if '|' in sample_prompts:
            prompts = sample_prompts.split('|')
        else:
            prompts = [sample_prompts]

        width, height = toml.loads(dataset)["datasets"][0]["resolution"]
        config_dict = {
            "sample_prompts": prompts,
            "dataset_repeats": dataset_repeats,
            "num_cpu_threads_per_process": 1,
            "pretrained_model_name_or_path": flux_models["transformer"],
            "clip_l": flux_models["clip_l"],
            "t5xxl": flux_models["t5"],
            "ae": flux_models["vae"],
            "save_model_as": "safetensors",
            "persistent_data_loader_workers": False,
            "max_data_loader_n_workers": 0,
            "seed": 42,
            "gradient_checkpointing": True,
            "dataset_config": dataset,
            "output_dir": output_dir,
            "output_name": f"{output_name}_rank{kwargs.get('network_dim')}_{args.save_precision}",
            "width" : int(width),
            "height" : int(height),

        }

        config_dict.update(kwargs)

        for key, value in config_dict.items():
            setattr(args, key, value)

        with torch.inference_mode(False):
            network_trainer = FluxNetworkTrainer()
            training_loop = network_trainer.init_train(args)

        final_output_path = os.path.join(output_dir, output_name)

        epochs_count = network_trainer.num_train_epochs

        trainer = {
            "network_trainer": network_trainer,
            "training_loop": training_loop,
        }
        return (trainer, epochs_count, final_output_path, args)
    
class FluxTrainLoop:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "network_trainer": ("NETWORKTRAINER",),
            "steps": ("INT", {"default": 1, "min": 1, "max": 10000, "step": 1, "tooltip": "the step point in training to validate/save"}),
             },
        }

    RETURN_TYPES = ("NETWORKTRAINER",)
    RETURN_NAMES = ("network_trainer",)
    FUNCTION = "train"
    CATEGORY = "FluxTrainer"

    def train(self, network_trainer, steps):
        with torch.inference_mode(False):
            training_loop = network_trainer["training_loop"]
            network_trainer = network_trainer["network_trainer"]
            initial_global_step = network_trainer.global_step

            target_global_step = network_trainer.global_step + steps
            pbar = comfy.utils.ProgressBar(steps)
            while network_trainer.global_step < target_global_step:
                steps_done = training_loop(
                    break_at_steps = target_global_step,
                    epoch = network_trainer.current_epoch.value,
                )
                pbar.update(steps_done)
               
                # Also break if the global steps have reached the max train steps
                if network_trainer.global_step >= network_trainer.args.max_train_steps:
                    break

            trainer = {
                "network_trainer": network_trainer,
                "training_loop": training_loop,
            }
        return (trainer, )

class FluxTrainSave:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "network_trainer": ("NETWORKTRAINER",),
            "save_state": ("BOOLEAN", {"default": False, "tooltip": "save the whole model state as well"}),
            "copy_to_comfy_lora_folder": ("BOOLEAN", {"default": False, "tooltip": "copy the lora model to the comfy lora folder"}),
             },
        }

    RETURN_TYPES = ("NETWORKTRAINER", "STRING", "INT",)
    RETURN_NAMES = ("network_trainer","lora_path", "steps",)
    FUNCTION = "save"
    CATEGORY = "FluxTrainer"

    def save(self, network_trainer, save_state, copy_to_comfy_lora_folder):
        import shutil
        with torch.inference_mode(False):
            trainer = network_trainer["network_trainer"]
            global_step = trainer.global_step
            
            ckpt_name = train_util.get_step_ckpt_name(trainer.args, "." + trainer.args.save_model_as, global_step)
            trainer.save_model(ckpt_name, trainer.accelerator.unwrap_model(trainer.network), global_step, trainer.current_epoch.value + 1)

            remove_step_no = train_util.get_remove_step_no(trainer.args, global_step)
            if remove_step_no is not None:
                remove_ckpt_name = train_util.get_step_ckpt_name(trainer.args, "." + trainer.args.save_model_as, remove_step_no)
                trainer.remove_model(remove_ckpt_name)

            if save_state:
                train_util.save_and_remove_state_stepwise(trainer.args, trainer.accelerator, global_step)

            lora_path = os.path.join(trainer.args.output_dir, ckpt_name)
            if copy_to_comfy_lora_folder:
                shutil.copy(lora_path, os.path.join(folder_paths.models_dir, "loras", "flux_trainer", ckpt_name))
        
            
        return (network_trainer, lora_path, global_step)
    
class FluxTrainEnd:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "network_trainer": ("NETWORKTRAINER",),
            "save_state": ("BOOLEAN", {"default": True}),
             },
        }

    RETURN_TYPES = ("STRING", "STRING",)
    RETURN_NAMES = ("lora_path", "metadata",)
    FUNCTION = "endtrain"
    CATEGORY = "FluxTrainer"

    def endtrain(self, network_trainer, save_state):
        with torch.inference_mode(False):
            training_loop = network_trainer["training_loop"]
            network_trainer = network_trainer["network_trainer"]
            
            network_trainer.metadata["ss_epoch"] = str(network_trainer.num_train_epochs)
            network_trainer.metadata["ss_training_finished_at"] = str(time.time())

            network = network_trainer.accelerator.unwrap_model(network_trainer.network)

            network_trainer.accelerator.end_training()

            if save_state:
                train_util.save_state_on_train_end(network_trainer.args, network_trainer.accelerator)

            ckpt_name = train_util.get_last_ckpt_name(network_trainer.args, "." + network_trainer.args.save_model_as)
            network_trainer.save_model(ckpt_name, network, network_trainer.global_step, network_trainer.num_train_epochs, force_sync_upload=True)
            logger.info("model saved.")

            final_output_lora_path = os.path.join(network_trainer.args.output_dir, network_trainer.args.output_name)

            # metadata
            metadata = json.dumps(network_trainer.metadata, indent=2)

            training_loop = None
            network_trainer = None
            mm.soft_empty_cache()
            
        return (final_output_lora_path, metadata)
    
class FluxTrainValidationSettings:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "steps": ("INT", {"default": 20, "min": 1, "max": 256, "step": 1, "tooltip": "sampling steps"}),
            "width": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 8, "tooltip": "image width"}),
            "height": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 8, "tooltip": "image height"}),
            "guidance_scale": ("FLOAT", {"default": 3.5, "min": 1.0, "max": 32.0, "step": 0.05, "tooltip": "guidance scale"}),
            "seed": ("INT", {"default": 42,"min": 0, "max": 0xffffffffffffffff, "step": 1}),
            },
        }

    RETURN_TYPES = ("VALSETTINGS", )
    RETURN_NAMES = ("validation_settings", )
    FUNCTION = "set"
    CATEGORY = "FluxTrainer"

    def set(self, **kwargs):
        validation_settings = kwargs
        print(validation_settings)

        return (validation_settings,)
        
class FluxTrainValidate:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "network_trainer": ("NETWORKTRAINER",),
            },
            "optional": {
                "validation_settings": ("VALSETTINGS",),
            }
        }

    RETURN_TYPES = ("NETWORKTRAINER", "IMAGE",)
    RETURN_NAMES = ("network_trainer", "validation_images",)
    FUNCTION = "validate"
    CATEGORY = "FluxTrainer"

    def validate(self, network_trainer, validation_settings=None):
        training_loop = network_trainer["training_loop"]
        network_trainer = network_trainer["network_trainer"]

        params = (
            network_trainer.accelerator, 
            network_trainer.args, 
            network_trainer.current_epoch.value, 
            network_trainer.global_step,
            network_trainer.unet,
            network_trainer.vae,
            network_trainer.text_encoder,
            network_trainer.sample_prompts_te_outputs,
            validation_settings
        )

        if not network_trainer.args.split_mode:
            image_tensors = flux_train_utils.sample_images(*params)
        else:
            image_tensors = network_trainer.sample_images_split_mode(*params)

        trainer = {
            "network_trainer": network_trainer,
            "training_loop": training_loop,
        }
        return (trainer, (0.5 * (image_tensors + 1.0)).cpu().float(),)
    
class VisualizeLoss:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "network_trainer": ("NETWORKTRAINER",),
            "plot_style": (plt.style.available,{"default": 'default', "tooltip": "matplotlib plot style"}),
            "window_size": ("INT", {"default": 100, "min": 0, "max": 10000, "step": 1, "tooltip": "the window size of the moving average"}),
            "normalize_y": ("BOOLEAN", {"default": True, "tooltip": "normalize the y-axis to 0"}),
            "width": ("INT", {"default": 768, "min": 256, "max": 4096, "step": 2, "tooltip": "width of the plot in pixels"}),
            "height": ("INT", {"default": 512, "min": 256, "max": 4096, "step": 2, "tooltip": "height of the plot in pixels"}),
             },
        }

    RETURN_TYPES = ("IMAGE", "FLOAT",)
    RETURN_NAMES = ("plot", "loss_list",)
    FUNCTION = "draw"
    CATEGORY = "FluxTrainer"

    def draw(self, network_trainer, window_size, plot_style, normalize_y, width, height):
        import numpy as np
        loss_values = network_trainer["network_trainer"].loss_recorder.global_loss_list

        # Apply moving average
        def moving_average(values, window_size):
            return np.convolve(values, np.ones(window_size) / window_size, mode='valid')
        if window_size > 0:
            loss_values = moving_average(loss_values, window_size)

        plt.style.use(plot_style)

        # Convert pixels to inches (assuming 100 pixels per inch)
        width_inches = width / 100
        height_inches = height / 100

        # Create a plot
        fig, ax = plt.subplots(figsize=(width_inches, height_inches))
        ax.plot(loss_values, label='Training Loss')
        ax.set_xlabel('Step')
        ax.set_ylabel('Loss')
        if normalize_y:
            plt.ylim(bottom=0)
        ax.set_title('Training Loss Over Time')
        ax.legend()
        ax.grid(True)

        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        plt.close(fig)
        buf.seek(0)

        image = Image.open(buf).convert('RGB')

        image_tensor = transforms.ToTensor()(image)
        image_tensor = image_tensor.unsqueeze(0).permute(0, 2, 3, 1).cpu().float()

        return image_tensor, loss_values,

class FluxKohyaInferenceSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "flux_models": ("TRAIN_FLUX_MODELS",),
            "lora_name": (folder_paths.get_filename_list("loras"), {"tooltip": "The name of the LoRA."}),
            "lora_method": (["apply", "merge"], {"tooltip": "whether to apply or merge the lora weights"}),
            "steps": ("INT", {"default": 20, "min": 1, "max": 256, "step": 1, "tooltip": "sampling steps"}),
            "width": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 8, "tooltip": "image width"}),
            "height": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 8, "tooltip": "image height"}),
            "guidance_scale": ("FLOAT", {"default": 3.5, "min": 1.0, "max": 32.0, "step": 0.05, "tooltip": "guidance scale"}),
            "seed": ("INT", {"default": 42,"min": 0, "max": 0xffffffffffffffff, "step": 1}),
            "use_fp8": ("BOOLEAN", {"default": True, "tooltip": "use fp8 weights"}),
            "prompt": ("STRING", {"multiline": True, "default": "illustration of a kitten", "tooltip": "prompt"}),
          
            },
        }

    RETURN_TYPES = ("IMAGE", )
    RETURN_NAMES = ("image", )
    FUNCTION = "sample"
    CATEGORY = "FluxTrainer"

    def sample(self, flux_models, lora_name, steps, width, height, guidance_scale, seed, prompt, use_fp8, lora_method):

        from .library import flux_utils as flux_utils
        from .library import strategy_flux as strategy_flux
        from .networks import lora_flux as lora_flux
        from typing import List, Optional, Callable
        from tqdm import tqdm
        import einops
        import math
        import accelerate
        import gc

        device = "cuda"
        apply_t5_attn_mask = True

        if use_fp8:
            accelerator = accelerate.Accelerator(mixed_precision="bf16")
            dtype = torch.float8_e4m3fn
        else:
            dtype = torch.float16
            accelerator = None
        loading_device = "cpu"
        ae_dtype = torch.bfloat16

        pretrained_model_name_or_path = flux_models["transformer"]
        clip_l = flux_models["clip_l"]
        t5xxl = flux_models["t5"]
        ae = flux_models["vae"]
        lora_path = folder_paths.get_full_path("loras", lora_name)

        # load clip_l
        logger.info(f"Loading clip_l from {clip_l}...")
        clip_l = flux_utils.load_clip_l(clip_l, None, loading_device)
        clip_l.eval()

        logger.info(f"Loading t5xxl from {t5xxl}...")
        t5xxl = flux_utils.load_t5xxl(t5xxl, None, loading_device)
        t5xxl.eval()

        if use_fp8:
            clip_l = accelerator.prepare(clip_l)
            t5xxl = accelerator.prepare(t5xxl)

        t5xxl_max_length = 512
        tokenize_strategy = strategy_flux.FluxTokenizeStrategy(t5xxl_max_length)
        encoding_strategy = strategy_flux.FluxTextEncodingStrategy()

        # DiT
        model = flux_utils.load_flow_model("dev", pretrained_model_name_or_path, dtype, loading_device)
        model.eval()
        logger.info(f"Casting model to {dtype}")
        model.to(dtype)  # make sure model is dtype
        if use_fp8:
            model = accelerator.prepare(model)

        # AE
        ae = flux_utils.load_ae("dev", ae, ae_dtype, loading_device)
        ae.eval()
        #if is_fp8(ae_dtype):
        #    ae = accelerator.prepare(ae)

        # LoRA
        lora_models: List[lora_flux.LoRANetwork] = []
        multiplier = 1.0

        lora_model, weights_sd = lora_flux.create_network_from_weights(
            multiplier, lora_path, ae, [clip_l, t5xxl], model, None, True
        )
        if lora_method == "merge":
            lora_model.merge_to([clip_l, t5xxl], model, weights_sd)
        elif lora_method == "apply":
            lora_model.apply_to([clip_l, t5xxl], model)
            info = lora_model.load_state_dict(weights_sd, strict=True)
            logger.info(f"Loaded LoRA weights from {lora_name}: {info}")
            lora_model.eval()
            lora_model.to(device)
        lora_models.append(lora_model)


        packed_latent_height, packed_latent_width = math.ceil(height / 16), math.ceil(width / 16)
        noise = torch.randn(
            1,
            packed_latent_height * packed_latent_width,
            16 * 2 * 2,
            device=device,
            dtype=ae_dtype,
            generator=torch.Generator(device=device).manual_seed(seed),
        )

        img_ids = flux_utils.prepare_img_ids(1, packed_latent_height, packed_latent_width)

        # prepare embeddings
        logger.info("Encoding prompts...")
        tokens_and_masks = tokenize_strategy.tokenize(prompt)
        clip_l = clip_l.to(device)
        t5xxl = t5xxl.to(device)
        with torch.no_grad():
            if use_fp8:
                clip_l.to(ae_dtype)
                t5xxl.to(ae_dtype)
                with accelerator.autocast():
                    _, t5_out, txt_ids, t5_attn_mask = encoding_strategy.encode_tokens(
                        tokenize_strategy, [clip_l, t5xxl], tokens_and_masks, apply_t5_attn_mask
                    )
            else:
                with torch.autocast(device_type=device.type, dtype=dtype):
                    l_pooled, _, _, _ = encoding_strategy.encode_tokens(tokenize_strategy, [clip_l, None], tokens_and_masks)
                with torch.autocast(device_type=device.type, dtype=dtype):
                    _, t5_out, txt_ids, t5_attn_mask = encoding_strategy.encode_tokens(
                        tokenize_strategy, [None, t5xxl], tokens_and_masks, apply_t5_attn_mask
                    )
        # NaN check
        if torch.isnan(l_pooled).any():
            raise ValueError("NaN in l_pooled")
                
        if torch.isnan(t5_out).any():
            raise ValueError("NaN in t5_out")

        
        clip_l = clip_l.cpu()
        t5xxl = t5xxl.cpu()
      
        gc.collect()
        torch.cuda.empty_cache()

        # generate image
        logger.info("Generating image...")
        model = model.to(device)
        print("MODEL DTYPE: ", model.dtype)

        img_ids = img_ids.to(device)
        t5_attn_mask = t5_attn_mask.to(device) if apply_t5_attn_mask else None
        def time_shift(mu: float, sigma: float, t: torch.Tensor):
            return math.exp(mu) / (math.exp(mu) + (1 / t - 1) ** sigma)


        def get_lin_function(x1: float = 256, y1: float = 0.5, x2: float = 4096, y2: float = 1.15) -> Callable[[float], float]:
            m = (y2 - y1) / (x2 - x1)
            b = y1 - m * x1
            return lambda x: m * x + b


        def get_schedule(
            num_steps: int,
            image_seq_len: int,
            base_shift: float = 0.5,
            max_shift: float = 1.15,
            shift: bool = True,
        ) -> list[float]:
            # extra step for zero
            timesteps = torch.linspace(1, 0, num_steps + 1)

            # shifting the schedule to favor high timesteps for higher signal images
            if shift:
                # eastimate mu based on linear estimation between two points
                mu = get_lin_function(y1=base_shift, y2=max_shift)(image_seq_len)
                timesteps = time_shift(mu, 1.0, timesteps)

            return timesteps.tolist()


        def denoise(
            model,
            img: torch.Tensor,
            img_ids: torch.Tensor,
            txt: torch.Tensor,
            txt_ids: torch.Tensor,
            vec: torch.Tensor,
            timesteps: list[float],
            guidance: float = 4.0,
            t5_attn_mask: Optional[torch.Tensor] = None,
        ):
            # this is ignored for schnell
            guidance_vec = torch.full((img.shape[0],), guidance, device=img.device, dtype=img.dtype)
            comfy_pbar = comfy.utils.ProgressBar(total=len(timesteps))
            for t_curr, t_prev in zip(tqdm(timesteps[:-1]), timesteps[1:]):
                t_vec = torch.full((img.shape[0],), t_curr, dtype=img.dtype, device=img.device)
                pred = model(
                    img=img,
                    img_ids=img_ids,
                    txt=txt,
                    txt_ids=txt_ids,
                    y=vec,
                    timesteps=t_vec,
                    guidance=guidance_vec,
                    txt_attention_mask=t5_attn_mask,
                )
                img = img + (t_prev - t_curr) * pred
                comfy_pbar.update(1)

            return img
        def do_sample(
            accelerator: Optional[accelerate.Accelerator],
            model,
            img: torch.Tensor,
            img_ids: torch.Tensor,
            l_pooled: torch.Tensor,
            t5_out: torch.Tensor,
            txt_ids: torch.Tensor,
            num_steps: int,
            guidance: float,
            t5_attn_mask: Optional[torch.Tensor],
            is_schnell: bool,
            device: torch.device,
            flux_dtype: torch.dtype,
        ):
            timesteps = get_schedule(num_steps, img.shape[1], shift=not is_schnell)

            # denoise initial noise
            if accelerator:
                with accelerator.autocast(), torch.no_grad():
                    x = denoise(
                        model, img, img_ids, t5_out, txt_ids, l_pooled, timesteps=timesteps, guidance=guidance, t5_attn_mask=t5_attn_mask
                    )
            else:
                with torch.autocast(device_type=device.type, dtype=flux_dtype), torch.no_grad():
                    x = denoise(
                        model, img, img_ids, t5_out, txt_ids, l_pooled, timesteps=timesteps, guidance=guidance, t5_attn_mask=t5_attn_mask
                    )

            return x
        
        x = do_sample(accelerator, model, noise, img_ids, l_pooled, t5_out, txt_ids, steps, guidance_scale, t5_attn_mask, False, device, dtype)
        
        model = model.cpu()
        gc.collect()
        torch.cuda.empty_cache()

        # unpack
        x = x.float()
        x = einops.rearrange(x, "b (h w) (c ph pw) -> b c (h ph) (w pw)", h=packed_latent_height, w=packed_latent_width, ph=2, pw=2)

        # decode
        logger.info("Decoding image...")
        ae = ae.to(device)
        with torch.no_grad():
            if use_fp8:
                with accelerator.autocast():
                    x = ae.decode(x)
            else:
                with torch.autocast(device_type=device.type, dtype=ae_dtype):
                    x = ae.decode(x)

        ae = ae.cpu()

        x = x.clamp(-1, 1)
        x = x.permute(0, 2, 3, 1)

        return ((0.5 * (x + 1.0)).cpu().float(),)   

class UploadToHuggingFace:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "network_trainer": ("NETWORKTRAINER",),
                "source_path": ("STRING", {"default": ""}),
                "repo_id": ("STRING",{"default": ""}),
                "revision": ("STRING", {"default": ""}),
                "private": ("BOOLEAN", {"default": True, "tooltip": "If creating a new repo, leave it private"}),
             },
             "optional": {
                "token": ("STRING", {"default": "","tooltip":"DO NOT LEAVE IN THE NODE or it might save in metadata, can also use the hf_token.json"}),
             }
        }

    RETURN_TYPES = ("NETWORKTRAINER", "STRING",)
    RETURN_NAMES = ("network_trainer","status",)
    FUNCTION = "upload"
    CATEGORY = "FluxTrainer"

    def upload(self, source_path, network_trainer, repo_id, private, revision, token=""):
        with torch.inference_mode(False):
            from huggingface_hub import HfApi
            
            if not token:
                with open(os.path.join(script_directory, "hf_token.json"), "r") as file:
                    token_data = json.load(file)
                token = token_data["hf_token"]
            print(token)

            # Save metadata to a JSON file
            directory_path = os.path.dirname(os.path.dirname(source_path))
            file_name = os.path.basename(source_path)

            metadata = network_trainer["network_trainer"].metadata
            metadata_file_path = os.path.join(directory_path, "metadata.json")
            with open(metadata_file_path, 'w') as f:
                json.dump(metadata, f, indent=4)

            repo_type = None
            api = HfApi(token=token)

            try:
                api.repo_info(
                    repo_id=repo_id, 
                    revision=revision if revision != "" else None, 
                    repo_type=repo_type)
                repo_exists = True
                logger.info(f"Repository {repo_id} exists.")
            except Exception as e:  # Catching a more specific exception would be better if you know what to expect
                repo_exists = False
                logger.error(f"Repository {repo_id} does not exist. Exception: {e}")
            
            if not repo_exists:
                try:
                    api.create_repo(repo_id=repo_id, repo_type=repo_type, private=private)
                except Exception as e:  # Checked for RepositoryNotFoundError, but other exceptions could be problematic
                    logger.error("===========================================")
                    logger.error(f"failed to create HuggingFace repo: {e}")
                    logger.error("===========================================")

            is_folder = (type(source_path) == str and os.path.isdir(source_path)) or (isinstance(source_path, Path) and source_path.is_dir())
            print(source_path, is_folder)

            try:
                if is_folder:
                    api.upload_folder(
                        repo_id=repo_id,
                        repo_type=repo_type,
                        folder_path=source_path,
                        path_in_repo=file_name,
                    )
                else:
                    api.upload_file(
                        repo_id=repo_id,
                        repo_type=repo_type,
                        path_or_fileobj=source_path,
                        path_in_repo=file_name,
                    )
                # Upload the metadata file separately if it's not a folder upload
                if not is_folder:
                    api.upload_file(
                        repo_id=repo_id,
                        repo_type=repo_type,
                        path_or_fileobj=str(metadata_file_path),
                        path_in_repo='metadata.json',
                    )
                status = "Uploaded to HuggingFace succesfully"
            except Exception as e:  # RuntimeErrorを確認済みだが他にあると困るので
                logger.error("===========================================")
                logger.error(f"failed to upload to HuggingFace / HuggingFaceへのアップロードに失敗しました : {e}")
                logger.error("===========================================")
                status = f"Failed to upload to HuggingFace {e}"
                
            return (network_trainer, status,)

NODE_CLASS_MAPPINGS = {
    "InitFluxLoRATraining": InitFluxLoRATraining,
    "InitFluxTraining": InitFluxTraining,
    "FluxTrainModelSelect": FluxTrainModelSelect,
    "TrainDatasetGeneralConfig": TrainDatasetGeneralConfig,
    "TrainDatasetAdd": TrainDatasetAdd,
    "FluxTrainLoop": FluxTrainLoop,
    "VisualizeLoss": VisualizeLoss,
    "FluxTrainValidate": FluxTrainValidate,
    "FluxTrainValidationSettings": FluxTrainValidationSettings,
    "FluxTrainEnd": FluxTrainEnd,
    "FluxTrainSave": FluxTrainSave,
    "FluxKohyaInferenceSampler": FluxKohyaInferenceSampler,
    "UploadToHuggingFace": UploadToHuggingFace,
    "OptimizerConfig": OptimizerConfig,
    "OptimizerConfigAdafactor": OptimizerConfigAdafactor
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "InitFluxLoRATraining": "Init Flux LoRA Training",
    "InitFluxTraining": "Init Flux Training",
    "FluxTrainModelSelect": "FluxTrain ModelSelect",
    "TrainDatasetGeneralConfig": "TrainDatasetGeneralConfig",
    "TrainDatasetAdd": "TrainDatasetAdd",
    "FluxTrainLoop": "Flux Train Loop",
    "VisualizeLoss": "Visualize Loss",
    "FluxTrainValidate": "Flux Train Validate",
    "FluxTrainValidationSettings": "Flux Train Validation Settings",
    "FluxTrainEnd": "Flux Train End",
    "FluxTrainSave": "Flux Train Save",
    "FluxKohyaInferenceSampler": "Flux Kohya Inference Sampler",
    "UploadToHuggingFace": "Upload To HuggingFace",
    "OptimizerConfig": "Optimizer Config",
    "OptimizerConfigAdafactor": "Optimizer Config Adafactor"
}
