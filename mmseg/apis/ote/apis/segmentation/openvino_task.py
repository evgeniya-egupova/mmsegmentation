# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import sys
import logging
import os
import tempfile
from addict import Dict as ADDict
from typing import Any, Dict, Tuple, List, Optional, Union

import cv2
import numpy as np

from ote_sdk.utils.segmentation_utils import (create_hard_prediction_from_soft_prediction,
                                              create_annotation_from_segmentation_map)
from ote_sdk.entities.datasets import DatasetEntity
from ote_sdk.entities.annotation import AnnotationSceneEntity, AnnotationSceneKind
from ote_sdk.entities.inference_parameters import InferenceParameters
from ote_sdk.entities.label import LabelEntity
from ote_sdk.entities.model import (
    ModelStatus,
    ModelEntity,
    ModelFormat,
    OptimizationMethod,
    ModelPrecision,
)
from ote_sdk.entities.optimization_parameters import OptimizationParameters
from ote_sdk.entities.resultset import ResultSetEntity
from ote_sdk.entities.task_environment import TaskEnvironment
from ote_sdk.usecases.evaluation.metrics_helper import MetricsHelper
from ote_sdk.usecases.exportable_code.inference import BaseOpenVINOInferencer
from ote_sdk.usecases.tasks.interfaces.evaluate_interface import IEvaluationTask
from ote_sdk.usecases.tasks.interfaces.inference_interface import IInferenceTask
from ote_sdk.usecases.tasks.interfaces.optimization_interface import IOptimizationTask, OptimizationType

# from compression.api import DataLoader
# from compression.engines.ie_engine import IEEngine
# from compression.graph import load_model, save_model
# from compression.graph.model_utils import compress_model_weights, get_nodes_by_type
# from compression.pipeline.initializer import create_pipeline

from openvino.tools.pot.api import DataLoader
from openvino.tools.pot.engines.ie_engine import IEEngine
from openvino.tools.pot.graph import load_model, save_model
from openvino.tools.pot.graph.model_utils import compress_model_weights, get_nodes_by_type
from openvino.tools.pot.pipeline.initializer import create_pipeline

from .configuration import OTESegmentationConfig


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
hdl = logging.StreamHandler(stream=sys.stdout)
hdl.setLevel(logging.INFO)
logger.addHandler(hdl)


def get_output(net, outputs, name):
    try:
        key = net.get_ov_name_for_tensor(name)
        assert key in outputs, f'"{key}" is not a valid output identifier'
    except KeyError:
        if name not in outputs:
            raise KeyError(f'Failed to identify output "{name}"')
        key = name

    return outputs[key]


class OpenVINOSegmentationInferencer(BaseOpenVINOInferencer):
    def __init__(
        self,
        hparams: OTESegmentationConfig,
        labels: List[LabelEntity],
        model_file: Union[str, bytes],
        weight_file: Union[str, bytes, None] = None,
        device: str = "CPU",
        num_requests: int = 1,
    ):
        """
        Inferencer implementation for OTESegmentation using OpenVINO backend.

        :param hparams: Hyper parameters that the model should use.
        :param model_file: Path to model to load, `.xml`, `.bin` or `.onnx` file.
        :param device: Device to run inference on, such as CPU, GPU or MYRIAD. Defaults to "CPU".
        :param num_requests: Maximum number of requests that the inferencer can make.
            Good value is the number of available cores. Defaults to 1.
        """

        super().__init__(model_file, weight_file, device, num_requests)

        self.labels = labels
        self.input_blob_name = 'input'
        self.n, self.c, self.h, self.w = self.net.input_info[self.input_blob_name].tensor_desc.dims
        self.keep_aspect_ratio_resize = False
        self.pad_value = 0
        self.soft_threshold = float(hparams.postprocessing.soft_threshold)
        self.blur_strength = int(hparams.postprocessing.blur_strength)

    @staticmethod
    def resize_image(image: np.ndarray, size: Tuple[int], keep_aspect_ratio: bool = False) -> np.ndarray:
        if not keep_aspect_ratio:
            resized_frame = cv2.resize(image, size)
        else:
            h, w = image.shape[:2]
            scale = min(size[1] / h, size[0] / w)
            resized_frame = cv2.resize(image, None, fx=scale, fy=scale)
        return resized_frame

    def pre_process(self, image: np.ndarray) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
        resized_image = self.resize_image(image, (self.w, self.h), self.keep_aspect_ratio_resize)
        meta = {'original_shape': image.shape,
                'resized_shape': resized_image.shape}

        h, w = resized_image.shape[:2]
        if h != self.h or w != self.w:
            resized_image = np.pad(resized_image,
                                   ((0, self.h - h), (0, self.w - w), (0, 0)),
                                   mode='constant',
                                   constant_values=self.pad_value)

        resized_image = resized_image.transpose((2, 0, 1))  # Change data layout from HWC to CHW
        resized_image = resized_image.reshape((self.n, self.c, self.h, self.w))
        dict_inputs = {self.input_blob_name: resized_image}

        return dict_inputs, meta

    def post_process(self, prediction: Dict[str, np.ndarray], metadata: Dict[str, Any]) -> AnnotationSceneEntity:
        pred_class_maps = prediction['output']
        assert pred_class_maps.shape[0] == 1
        pred_class_map = pred_class_maps[0]

        soft_prediction = np.transpose(pred_class_map, axes=(1, 2, 0))

        hard_prediction = create_hard_prediction_from_soft_prediction(
            soft_prediction=soft_prediction,
            soft_threshold=self.soft_threshold,
            blur_strength=self.blur_strength
        )

        label_dictionary = {i + 1: self.labels[i] for i in range(len(self.labels))}
        annotations = create_annotation_from_segmentation_map(
            hard_prediction=hard_prediction,
            soft_prediction=soft_prediction,
            label_map=label_dictionary
        )

        return AnnotationSceneEntity(
            kind=AnnotationSceneKind.PREDICTION,
            annotations=annotations
        )

    def forward(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        return self.model.infer(inputs)


class OTEOpenVinoDataLoader(DataLoader):
    def __init__(self, dataset: DatasetEntity, inferencer: BaseOpenVINOInferencer):
        self.dataset = dataset
        self.inferencer = inferencer

    def __getitem__(self, index):
        image = self.dataset[index].numpy
        annotation = self.dataset[index].annotation_scene
        inputs, metadata = self.inferencer.pre_process(image)

        return (index, annotation), inputs, metadata

    def __len__(self):
        return len(self.dataset)


class OpenVINOSegmentationTask(IInferenceTask, IEvaluationTask, IOptimizationTask):
    def __init__(self,
                 task_environment: TaskEnvironment):
        self.task_environment = task_environment
        self.model = self.task_environment.model
        self.inferencer = self.load_inferencer()

    @property
    def hparams(self):
        return self.task_environment.get_hyper_parameters(OTESegmentationConfig)

    def load_inferencer(self) -> OpenVINOSegmentationInferencer:
        labels = self.task_environment.label_schema.get_labels(include_empty=False)
        return OpenVINOSegmentationInferencer(self.hparams,
                                              labels,
                                              self.model.get_data("openvino.xml"),
                                              self.model.get_data("openvino.bin"))

    def infer(self,
              dataset: DatasetEntity,
              inference_parameters: Optional[InferenceParameters] = None) -> DatasetEntity:
        from tqdm import tqdm
        for dataset_item in tqdm(dataset):
            dataset_item.annotation_scene = self.inferencer.predict(dataset_item.numpy)
        return dataset

    def evaluate(self,
                 output_result_set: ResultSetEntity,
                 evaluation_metric: Optional[str] = None):
        logger.info('Computing mDice')
        metrics = MetricsHelper.compute_dice_averaged_over_pixels(
            output_result_set
        )
        logger.info(f"mDice after evaluation: {metrics.overall_dice.value}")

        output_result_set.performance = metrics.get_performance()

    def optimize(self,
                 optimization_type: OptimizationType,
                 dataset: DatasetEntity,
                 output_model: ModelEntity,
                 optimization_parameters: Optional[OptimizationParameters]):

        if optimization_type is not OptimizationType.POT:
            raise ValueError("POT is the only supported optimization type for OpenVino models")

        data_loader = OTEOpenVinoDataLoader(dataset, self.inferencer)

        with tempfile.TemporaryDirectory() as tempdir:
            xml_path = os.path.join(tempdir, "model.xml")
            bin_path = os.path.join(tempdir, "model.bin")
            with open(xml_path, "wb") as f:
                f.write(self.model.get_data("openvino.xml"))
            with open(bin_path, "wb") as f:
                f.write(self.model.get_data("openvino.bin"))

            model_config = ADDict({
                'model_name': 'openvino_model',
                'model': xml_path,
                'weights': bin_path
            })

            model = load_model(model_config)

            if get_nodes_by_type(model, ['FakeQuantize']):
                logger.warning("Model is already optimized by POT")
                output_model.model_status = ModelStatus.FAILED
                return

        engine_config = ADDict({
            'device': 'CPU'
        })

        stat_subset_size = self.hparams.pot_parameters.stat_subset_size
        preset = self.hparams.pot_parameters.preset.name.lower()
        preset = "mixed"
        logger.info(f'Preset: {preset}')

        algorithms = [
            {
                'name': 'DefaultQuantization',
                'params': {
                    'target_device': 'ANY',
                    'maximal_drop': 0.01,
                    'preset': preset,
                    "range_estimator": {
                        "preset": "quantile"
                    },
                    'stat_subset_size': min(stat_subset_size, len(data_loader)),
                    # 'use_fast_bias': False,
                    "ignored": {
                        "scope":[
                            "Mul_74",
                            "Mul_74",
                            "Mul_94",
                            "Mul_195",
                            "Mul_215",
                            "Mul_346",
                            "Mul_366",
                            "Mul_467",
                            "Mul_487",
                            "Mul_623",
                            "Mul_643",
                            "Mul_663",
                            "Mul_802",
                            "Mul_822",
                            "Mul_842",
                            "Mul_1069",
                            "Mul_1089",
                            "Mul_1109",
                            "Mul_1248",
                            "Mul_1268",
                            "Mul_1288",
                            "Mul_1515",
                            "Mul_1535",
                            "Mul_1555",
                            "Mul_1694",
                            "Mul_1714",
                            "Mul_1734",
                            "Mul_1961",
                            "Mul_1981",
                            "Mul_2001",
                            "Mul_2140",
                            "Mul_2160",
                            "Mul_2180",
                            "Mul_2412",
                            "Mul_2432",
                            "Mul_2452",
                            "Mul_2472",
                            "Mul_2649",
                            "Mul_2669",
                            "Mul_2689",
                            "Mul_2709",
                            "Mul_3065",
                            "Mul_3085",
                            "Mul_3105",
                            "Mul_3125",
                            "Mul_3302",
                            "Mul_3322",
                            "Mul_3342",
                            "Mul_3362",
                            "Mul_3781"
                        ]
                        # "scope": [
                        #     'Mul_102',
                        #     'Mul_108',
                        #     'Mul_223',
                        #     'Mul_229',
                        #     'Mul_374',
                        #     'Mul_380',
                        #     'Mul_495',
                        #     'Mul_501',
                        #     'Mul_672',
                        #     'Mul_678',
                        #     'Mul_684',
                        #     'Mul_851',
                        #     'Mul_857',
                        #     'Mul_863',
                        #     'Mul_1118',
                        #     'Mul_1124',
                        #     'Mul_1130',
                        #     'Mul_1297',
                        #     'Mul_1303',
                        #     'Mul_1309',
                        #     'Mul_1564',
                        #     'Mul_1570',
                        #     'Mul_1576',
                        #     'Mul_1743',
                        #     'Mul_1749',
                        #     'Mul_1755',
                        #     'Mul_2010',
                        #     'Mul_2016',
                        #     'Mul_2022',
                        #     'Mul_2189',
                        #     'Mul_2195',
                        #     'Mul_2201',
                        #     'Mul_2482',
                        #     'Mul_2488',
                        #     'Mul_2494',
                        #     'Mul_2500',
                        #     'Mul_2719',
                        #     'Mul_2725',
                        #     'Mul_2731',
                        #     'Mul_2737',
                        #     'Mul_3135',
                        #     'Mul_3141',
                        #     'Mul_3147',
                        #     'Mul_3153',
                        #     'Mul_3372',
                        #     'Mul_3378',
                        #     'Mul_3384',
                        #     'Mul_3390']
                        # "scope": ['Resize_3896',
                        #           'Resize_3877'],
                        # "operations": [{
                        #     "type": "Multiply"
                        # }]
                    }
                }
            }
        ]

        engine = IEEngine(config=engine_config, data_loader=data_loader, metric=None)

        pipeline = create_pipeline(algorithms, engine)

        compressed_model = pipeline.run(model)

        compress_model_weights(compressed_model)

        with tempfile.TemporaryDirectory() as tempdir:
            # tempdir = ''
            save_model(compressed_model, tempdir, model_name="model")
            with open(os.path.join(tempdir, "model.xml"), "rb") as f:
                output_model.set_data("openvino.xml", f.read())
            with open(os.path.join(tempdir, "model.bin"), "rb") as f:
                output_model.set_data("openvino.bin", f.read())

        # set model attributes for quantized model
        output_model.model_status = ModelStatus.SUCCESS
        output_model.model_format = ModelFormat.OPENVINO
        output_model.optimization_type = OptimizationType.POT
        output_model.optimization_methods = [OptimizationMethod.QUANTIZATION]
        output_model.precision = [ModelPrecision.INT8]

        self.model = output_model
        self.inferencer = self.load_inferencer()
