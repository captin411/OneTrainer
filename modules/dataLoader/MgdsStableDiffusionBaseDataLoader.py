import json

from mgds.DebugDataLoaderModules import DecodeVAE, SaveImage, SaveText, DecodeTokens
from mgds.DiffusersDataLoaderModules import *
from mgds.GenericDataLoaderModules import *
from mgds.MGDS import MGDS, TrainDataLoader, OutputPipelineModule
from mgds.TransformersDataLoaderModules import *

from modules.model.StableDiffusionModel import StableDiffusionModel
from modules.util import path_util
from modules.util.TrainProgress import TrainProgress
from modules.util.args.TrainArgs import TrainArgs


class MgdsStablDiffusionBaseDataLoader:
    def __init__(
            self,
            args: TrainArgs,
            model: StableDiffusionModel,
            train_progress: TrainProgress,
    ):
        with open(args.concept_file_name, 'r') as f:
            concepts = json.load(f)

        self.ds = self.create_dataset(
            args=args,
            model=model,
            concepts=concepts,
            train_progress=train_progress,
        )
        self.dl = TrainDataLoader(self.ds, args.batch_size)


    def _enumerate_input_modules(self, args: TrainArgs) -> list:
        supported_extensions = path_util.supported_image_extensions()

        collect_paths = CollectPaths(
            concept_in_name='concept', path_in_name='path', name_in_name='name', path_out_name='image_path', concept_out_name='concept', extensions=supported_extensions, include_postfix=None, exclude_postfix=['-masklabel']
        )

        mask_path = ModifyPath(in_name='image_path', out_name='mask_path', postfix='-masklabel', extension='.png')
        sample_prompt_path = ModifyPath(in_name='image_path', out_name='sample_prompt_path', postfix='', extension='.txt')

        modules = [collect_paths, sample_prompt_path]

        if args.masked_training:
            modules.append(mask_path)

        return modules


    def _load_input_modules(self, args: TrainArgs, model: StableDiffusionModel) -> list:
        load_image = LoadImage(path_in_name='image_path', image_out_name='image', range_min=-1.0, range_max=1.0)

        generate_mask = GenerateImageLike(image_in_name='image', image_out_name='mask', color=255, range_min=0, range_max=1, channels=1)
        load_mask = LoadImage(path_in_name='mask_path', image_out_name='mask', range_min=0, range_max=1, channels=1)

        generate_depth = GenerateDepth(path_in_name='image_path', image_out_name='depth', image_depth_processor=model.image_depth_processor, depth_estimator=model.depth_estimator)

        load_sample_prompts = LoadMultipleTexts(path_in_name='sample_prompt_path', texts_out_name='sample_prompts')
        load_concept_prompts = LoadMultipleTexts(path_in_name='concept.prompt_path', texts_out_name='concept_prompts')
        filename_prompt = GetFilename(path_in_name='image_path', filename_out_name='filename_prompt', include_extension=False)
        select_prompt_input = SelectInput(setting_name='concept.prompt_source', out_name='prompts', setting_to_in_name_map={
            'sample': 'sample_prompts',
            'concept': 'concept_prompts',
            'filename': 'filename_prompts',
        }, default_in_name='sample_prompts')
        select_random_text = SelectRandomText(texts_in_name='prompts', text_out_name='prompt')

        modules = [load_image, load_sample_prompts, load_concept_prompts, filename_prompt, select_prompt_input, select_random_text]

        if args.masked_training:
            modules.append(generate_mask)
            modules.append(load_mask)
        elif args.model_type.has_mask_input():
            modules.append(generate_mask)

        if args.model_type.has_depth_input():
            modules.append(generate_depth)

        return modules


    def _mask_augmentation_modules(self, args: TrainArgs) -> list:
        inputs = ['image']

        if args.model_type.has_depth_input():
            inputs.append('depth')

        circular_mask_shrink = RandomCircularMaskShrink(mask_name='mask', shrink_probability=1.0, shrink_factor_min=0.2, shrink_factor_max=1.0, enabled_in_name='settings.enable_random_circular_mask_shrink')
        random_mask_rotate_crop = RandomMaskRotateCrop(mask_name='mask', additional_names=inputs, min_size=args.resolution, min_padding_percent=10, max_padding_percent=30, max_rotate_angle=20, enabled_in_name='settings.enable_random_mask_rotate_crop')

        modules = []

        if args.masked_training or args.model_type.has_mask_input():
            modules.append(circular_mask_shrink)

        if args.masked_training or args.model_type.has_mask_input():
            modules.append(random_mask_rotate_crop)

        return modules


    def _aspect_bucketing_in(self, args: TrainArgs):
        calc_aspect = CalcAspect(image_in_name='image', resolution_out_name='original_resolution')

        aspect_bucketing = AspectBucketing(
            target_resolution=args.resolution,
            resolution_in_name='original_resolution',
            scale_resolution_out_name='scale_resolution', crop_resolution_out_name='crop_resolution', possible_resolutions_out_name='possible_resolutions'
        )

        single_aspect_calculation = SingleAspectCalculation(
            target_resolution=args.resolution,
            resolution_in_name='original_resolution',
            scale_resolution_out_name='scale_resolution', crop_resolution_out_name='crop_resolution',
            possible_resolutions_out_name='possible_resolutions'
        )

        modules = [calc_aspect]

        if args.aspect_ratio_bucketing:
            modules.append(aspect_bucketing)
        else:
            modules.append(single_aspect_calculation)

        return modules


    def _crop_modules(self, args: TrainArgs):
        scale_crop_image = ScaleCropImage(image_in_name='image', scale_resolution_in_name='scale_resolution', crop_resolution_in_name='crop_resolution', image_out_name='image', enable_crop_jitter_in_name='concept.enable_crop_jitter')
        scale_crop_mask = ScaleCropImage(image_in_name='mask', scale_resolution_in_name='scale_resolution', crop_resolution_in_name='crop_resolution', image_out_name='mask', enable_crop_jitter_in_name='concept.enable_crop_jitter')
        scale_crop_depth = ScaleCropImage(image_in_name='depth', scale_resolution_in_name='scale_resolution', crop_resolution_in_name='crop_resolution', image_out_name='depth', enable_crop_jitter_in_name='concept.enable_crop_jitter')

        modules = [scale_crop_image]

        if args.masked_training or args.model_type.has_mask_input():
            modules.append(scale_crop_mask)

        if args.model_type.has_depth_input():
            modules.append(scale_crop_depth)

        return modules


    def _inpainting_modules(self, args: TrainArgs):
        conditioning_image = GenerateMaskedConditioningImage(image_in_name='image', mask_in_name='mask', image_out_name='conditioning_image')

        modules = []

        if args.model_type.has_conditioning_image_input():
            modules.append(conditioning_image)

        return modules


    def _augmentation_modules(self, args: TrainArgs):
        inputs = ['image']

        if args.masked_training or args.model_type.has_mask_input():
            inputs.append('mask')

        if args.model_type.has_conditioning_image_input():
            inputs.append('conditioning_image')

        if args.model_type.has_depth_input():
            inputs.append('depth')

        random_flip = RandomFlip(names=inputs, enabled_in_name='concept.enable_random_flip')

        modules = [random_flip]

        return modules


    def _preparation_modules(self, args: TrainArgs, model: StableDiffusionModel):
        image = EncodeVAE(in_name='image', out_name='latent_image_distribution', vae=model.vae)
        mask = Downscale(in_name='mask', out_name='latent_mask', factor=8)
        conditioning_image = EncodeVAE(in_name='conditioning_image', out_name='latent_conditioning_image_distribution', vae=model.vae)
        depth = Downscale(in_name='depth', out_name='latent_depth', factor=8)
        tokens = Tokenize(in_name='prompt', out_name='tokens', tokenizer=model.tokenizer)

        modules = [image, tokens]

        if args.masked_training or args.model_type.has_mask_input():
            modules.append(mask)

        if args.model_type.has_conditioning_image_input():
            modules.append(conditioning_image)

        if args.model_type.has_depth_input():
            modules.append(depth)

        return modules


    def _cache_modules(self, args: TrainArgs):
        split_names = ['latent_image_distribution']

        if args.masked_training or args.model_type.has_mask_input():
            split_names.append('latent_mask')

        if args.model_type.has_conditioning_image_input():
            split_names.append('latent_conditioning_image_distribution')

        if args.model_type.has_depth_input():
            split_names.append('latent_depth')

        aggregate_names = ['crop_resolution', 'image_path']

        disk_cache = DiskCache(cache_dir=args.cache_dir, split_names=split_names, aggregate_names=aggregate_names, cached_epochs=args.latent_caching_epochs)

        modules = []

        if args.latent_caching:
            modules.append(disk_cache)

        return modules


    def _output_modules(self, args: TrainArgs, model: StableDiffusionModel):
        output_names = ['latent_image', 'tokens', 'image_path']

        if args.masked_training or args.model_type.has_mask_input():
            output_names.append('latent_mask')

        if args.model_type.has_conditioning_image_input():
            output_names.append('latent_conditioning_image')

        if args.model_type.has_depth_input():
            output_names.append('latent_depth')

        image_sample = SampleVAEDistribution(in_name='latent_image_distribution', out_name='latent_image', mode='mean')
        conditioning_image_sample = SampleVAEDistribution(in_name='latent_conditioning_image_distribution', out_name='latent_conditioning_image', mode='mean')
        mask_remove = RandomLatentMaskRemove(
            latent_mask_name='latent_mask', latent_conditioning_image_name='latent_conditioning_image',
            replace_probability=args.unmasked_probability, vae=model.vae, possible_resolutions_in_name='possible_resolutions'
        )
        batch_sorting = AspectBatchSorting(resolution_in_name='crop_resolution', names=output_names, batch_size=args.batch_size, sort_resolutions_for_each_epoch=True)
        output = OutputPipelineModule(names=output_names)

        modules = [image_sample]

        if args.model_type.has_conditioning_image_input():
            modules.append(conditioning_image_sample)

        if args.model_type.has_mask_input():
            modules.append(mask_remove)

        if args.aspect_ratio_bucketing:
            modules.append(batch_sorting)

        modules.append(output)

        return modules


    def create_dataset(
            self,
            args: TrainArgs,
            model: StableDiffusionModel,
            concepts: list[dict],
            train_progress: TrainProgress,
    ):
        enumerate_input = self._enumerate_input_modules(args)
        load_input = self._load_input_modules(args, model)
        mask_augmentation = self._mask_augmentation_modules(args)
        aspect_bucketing_in = self._aspect_bucketing_in(args)
        crop_modules = self._crop_modules(args)
        inpainting_modules = self._inpainting_modules(args)
        augmentation_modules = self._augmentation_modules(args)
        preparation_modules = self._preparation_modules(args, model)
        cache_modules = self._cache_modules(args)
        output_modules = self._output_modules(args, model)

        debug_dir = os.path.join(args.debug_dir, "dataloader")
        debug_modules = [
            DecodeVAE(in_name='latent_image', out_name='decoded_image', vae=model.vae),
            DecodeVAE(in_name='latent_conditioning_image', out_name='decoded_conditioning_image', vae=model.vae),
            Upscale(in_name='latent_mask', out_name='decoded_mask', factor=8),
            DecodeTokens(in_name='tokens', out_name='decoded_prompt', tokenizer=model.tokenizer),
            SaveImage(image_in_name='decoded_image', original_path_in_name='image_path', path=debug_dir, in_range_min=-1, in_range_max=1),
            SaveImage(image_in_name='decoded_conditioning_image', original_path_in_name='image_path', path=debug_dir, in_range_min=-1, in_range_max=1),
            # SaveImage(image_in_name='latent_mask', original_path_in_name='image_path', path=debug_dir, in_range_min=0, in_range_max=1),
            SaveImage(image_in_name='decoded_mask', original_path_in_name='image_path', path=debug_dir, in_range_min=0, in_range_max=1),
            # SaveImage(image_in_name='latent_depth', original_path_in_name='image_path', path=debug_dir, in_range_min=-1, in_range_max=1),
            SaveText(text_in_name='decoded_prompt', original_path_in_name='image_path', path=debug_dir),

            # These modules don't really work, since they are inserted after a sorting operation that does not include this data
            # SaveImage(image_in_name='mask', original_path_in_name='image_path', path=debug_dir, in_range_min=0, in_range_max=1),
            # SaveImage(image_in_name='depth', original_path_in_name='image_path', path=debug_dir, in_range_min=-1, in_range_max=1),
            # SaveImage(image_in_name='image', original_path_in_name='image_path', path=debug_dir, in_range_min=-1, in_range_max=1),
        ]

        settings = {
            "enable_random_circular_mask_shrink": args.circular_mask_generation,
            "enable_random_mask_rotate_crop": args.random_rotate_and_crop,
        }

        ds = MGDS(
            args.train_device,
            args.train_dtype.torch_dtype(),
            args.train_dtype.enable_mixed_precision(),
            concepts,
            settings,
            [
                enumerate_input,
                load_input,
                mask_augmentation,
                aspect_bucketing_in,
                crop_modules,
                inpainting_modules,
                augmentation_modules,
                preparation_modules,
                cache_modules,
                output_modules,

                debug_modules if args.debug_mode else None,  # inserted before output_modules, which contains a sorting operation
            ],
            batch_size=args.batch_size,
            initial_epoch=train_progress.epoch,
            initial_epoch_sample=train_progress.epoch_sample,
        )

        return ds
