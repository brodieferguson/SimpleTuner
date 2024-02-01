from transformers import PretrainedConfig
from diffusers import UNet2DConditionModel, DiffusionPipeline
from diffusers.utils import (
    convert_state_dict_to_diffusers,
    convert_unet_state_dict_to_peft,
)
from peft import set_peft_model_state_dict
from peft.utils import get_peft_model_state_dict
from helpers.training.state_tracker import StateTracker
from helpers.training.wrappers import unwrap_model
import os, logging, shutil

logger = logging.getLogger("legacy.sd_files")
logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL", "INFO"))


def import_model_class_from_model_name_or_path(
    pretrained_model_name_or_path: str, revision: str
):
    text_encoder_config = PretrainedConfig.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="text_encoder",
        revision=revision,
    )
    model_class = text_encoder_config.architectures[0]

    if model_class == "CLIPTextModel":
        from transformers import CLIPTextModel

        return CLIPTextModel
    elif model_class == "RobertaSeriesModelWithTransformation":
        from diffusers.pipelines.alt_diffusion.modeling_roberta_series import (
            RobertaSeriesModelWithTransformation,
        )

        return RobertaSeriesModelWithTransformation
    else:
        raise ValueError(f"{model_class} is not supported.")


def register_file_hooks(
    args, accelerator, unet, text_encoder, text_encoder_cls, use_deepspeed_optimizer
):
    # create custom saving & loading hooks so that `accelerator.save_state(...)` serializes in a nice format
    def save_model_hook(models, weights, output_dir):
        StateTracker.save_training_state(
            os.path.join(output_dir, "training_state.json")
        )
        if StateTracker.get_args().model_type == "lora":
            # there are only two options here. Either are just the unet attn processor layers
            # or there are the unet and text encoder atten layers
            unet_lora_layers_to_save = None
            text_encoder_lora_layers_to_save = None

            for model in models:
                if isinstance(model, type(unwrap_model(accelerator, unet))):
                    unet_lora_layers_to_save = convert_state_dict_to_diffusers(
                        get_peft_model_state_dict(model)
                    )
                elif isinstance(model, type(accelerator.unwrap_model(text_encoder))):
                    text_encoder_lora_layers_to_save = convert_state_dict_to_diffusers(
                        get_peft_model_state_dict(model)
                    )
                elif not use_deepspeed_optimizer:
                    raise ValueError(f"unexpected save model: {model.__class__}")

                # make sure to pop weight so that corresponding model is not saved again
                if weights:
                    weights.pop()

            DiffusionPipeline.save_lora_weights(
                output_dir,
                unet_lora_layers=unet_lora_layers_to_save,
                text_encoder_lora_layers=text_encoder_lora_layers_to_save,
            )
            return
        for model in models:
            # _before_ saving state, check if this save would set us over the `checkpoints_total_limit`
            if args.checkpoints_total_limit is not None:
                checkpoints = os.listdir(args.output_dir)
                checkpoints = [d for d in checkpoints if d.startswith("checkpoint")]
                checkpoints = sorted(checkpoints, key=lambda x: int(x.split("-")[1]))

                # before we save the new checkpoint, we need to have at _most_ `checkpoints_total_limit - 1` checkpoints
                if len(checkpoints) >= args.checkpoints_total_limit:
                    num_to_remove = len(checkpoints) - args.checkpoints_total_limit + 1
                    removing_checkpoints = checkpoints[0:num_to_remove]

                    logger.info(
                        f"{len(checkpoints)} checkpoints already exist, removing {len(removing_checkpoints)} checkpoints"
                    )
                    logger.info(
                        f"removing checkpoints: {', '.join(removing_checkpoints)}"
                    )

                    for removing_checkpoint in removing_checkpoints:
                        removing_checkpoint = os.path.join(
                            args.output_dir, removing_checkpoint
                        )
                        shutil.rmtree(removing_checkpoint)
            sub_dir = (
                "unet"
                if isinstance(model, type(unwrap_model(accelerator, unet)))
                else "text_encoder"
            )
            model.save_pretrained(os.path.join(output_dir, sub_dir))

            # make sure to pop weight so that corresponding model is not saved again
            weights.pop()

    def load_model_hook(models, input_dir):
        training_state_path = os.path.join(input_dir, "training_state.json")
        if os.path.exists(training_state_path):
            StateTracker.load_training_state(training_state_path)
        else:
            logger.warning(
                f"Could not find training_state.json in checkpoint dir {input_dir}"
            )
        while len(models) > 0:
            # pop models so that they are not loaded again
            model = models.pop()
            if isinstance(model, type(accelerator.unwrap_model(text_encoder))):
                # load transformers style into model
                load_model = text_encoder_cls.from_pretrained(
                    input_dir, subfolder="text_encoder"
                )
                model.config = load_model.config
            else:
                # load diffusers style into model
                load_model = UNet2DConditionModel.from_pretrained(
                    input_dir, subfolder="unet"
                )
                model.register_to_config(**load_model.config)

            model.load_state_dict(load_model.state_dict())
            del load_model

    accelerator.register_save_state_pre_hook(save_model_hook)
    accelerator.register_load_state_pre_hook(load_model_hook)
