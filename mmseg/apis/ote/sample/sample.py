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

import argparse
import logging
import os.path as osp
import sys

from time import time

from ote_sdk.configuration.helper import create
from ote_sdk.entities.datasets import DatasetEntity
from ote_sdk.entities.inference_parameters import InferenceParameters
from ote_sdk.entities.label_schema import LabelSchemaEntity
from ote_sdk.entities.model import ModelEntity, ModelStatus
from ote_sdk.entities.model_template import parse_model_template
from ote_sdk.entities.optimization_parameters import OptimizationParameters
from ote_sdk.entities.resultset import ResultSetEntity
from ote_sdk.entities.subset import Subset
from ote_sdk.usecases.tasks.interfaces.export_interface import ExportType
from ote_sdk.usecases.tasks.interfaces.optimization_interface import OptimizationType
from ote_sdk.entities.task_environment import TaskEnvironment

from mmseg.apis.ote.apis.segmentation.config_utils import set_values_as_default
from mmseg.apis.ote.apis.segmentation.ote_utils import get_task_class
from mmseg.apis.ote.extension.datasets.mmdataset import load_dataset_items


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
hdl = logging.StreamHandler(stream=sys.stdout)
hdl.setLevel(logging.INFO)
logger.addHandler(hdl)



def parse_args():
    parser = argparse.ArgumentParser(description='Sample showcasing the new API')
    parser.add_argument('template_file_path', help='path to template file')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--export', action='store_true')
    return parser.parse_args()


def main(args):
    logger.info('Initialize dataset')
    labels_list = []
    items = load_dataset_items(
        ann_file_path=osp.join(args.data_dir, 'annotations/training'),
        data_root_dir=osp.join(args.data_dir, 'images/training'),
        subset=Subset.TRAINING,
        labels_list=labels_list)
    items.extend(load_dataset_items(
        ann_file_path=osp.join(args.data_dir, 'annotations/validation'),
        data_root_dir=osp.join(args.data_dir, 'images/validation'),
        subset=Subset.VALIDATION,
        labels_list=labels_list))
    items.extend(load_dataset_items(
        ann_file_path=osp.join(args.data_dir, 'annotations/validation'),
        data_root_dir=osp.join(args.data_dir, 'images/validation'),
        subset=Subset.TESTING,
        labels_list=labels_list))
    dataset = DatasetEntity(items=items)

    labels_schema = LabelSchemaEntity.from_labels(labels_list)

    logger.info(f'Train dataset: {len(dataset.get_subset(Subset.TRAINING))} items')
    logger.info(f'Validation dataset: {len(dataset.get_subset(Subset.VALIDATION))} items')

    logger.info('Load model template')
    model_template = parse_model_template(args.template_file_path)

    hyper_parameters = model_template.hyper_parameters.data
    set_values_as_default(hyper_parameters)

    logger.info('Setup environment')
    params = create(hyper_parameters)
    logger.info('Set hyperparameters')
    params.learning_parameters.learning_rate_fixed_iters = 30
    params.learning_parameters.learning_rate_warmup_iters = 30
    params.learning_parameters.num_iters = 30
    environment = TaskEnvironment(model=None,
                                  hyper_parameters=params,
                                  label_schema=labels_schema,
                                  model_template=model_template)

    logger.info('Create base Task')
    task_impl_path = model_template.entrypoints.base
    task_cls = get_task_class(task_impl_path)
    task = task_cls(task_environment=environment)

    logger.info('Train model')
    output_model = ModelEntity(dataset,
                               environment.get_model_configuration(),
                               model_status=ModelStatus.NOT_READY)
    # task.train(dataset, output_model)

    logger.info('Get predictions on the validation set')
    validation_dataset = dataset.get_subset(Subset.VALIDATION)
    predicted_validation_dataset = task.infer(validation_dataset.with_empty_annotations(),
                                              InferenceParameters(is_evaluation=True))
    resultset = ResultSetEntity(model=output_model,
                                ground_truth_dataset=validation_dataset,
                                prediction_dataset=predicted_validation_dataset)
    logger.info('Estimate quality on validation set')
    task.evaluate(resultset)
    logger.info(str(resultset.performance))

    if args.export:
        start = time()
        logger.info('Export model')
        exported_model = ModelEntity(dataset,
                                     environment.get_model_configuration(),
                                     model_status=ModelStatus.NOT_READY)
        task.export(ExportType.OPENVINO, exported_model)
        finish = time()
        print(f'Export time: {finish - start}')

        start = time()
        logger.info('Create OpenVINO Task')
        environment.model = exported_model
        openvino_task_impl_path = model_template.entrypoints.openvino
        openvino_task_cls = get_task_class(openvino_task_impl_path)
        openvino_task = openvino_task_cls(environment)
        finish = time()
        print(f'Openvino task time: {finish - start}')

        start = time()
        logger.info('Get predictions on the validation set')
        predicted_validation_dataset = openvino_task.infer(validation_dataset.with_empty_annotations(),
                                                           InferenceParameters(is_evaluation=True))
        resultset = ResultSetEntity(model=output_model,
                                    ground_truth_dataset=validation_dataset,
                                    prediction_dataset=predicted_validation_dataset)
        logger.info('Estimate quality on validation set')
        openvino_task.evaluate(resultset)
        logger.info(str(resultset.performance))
        finish = time()
        print(f'Validation time: {finish - start}')
        start = time()

        logger.info('Run POT optimization')
        optimized_model = ModelEntity(dataset,
                                      environment.get_model_configuration(),
                                      model_status=ModelStatus.NOT_READY)
        openvino_task.optimize(OptimizationType.POT,
                               dataset.get_subset(Subset.TRAINING),
                               optimized_model,
                               OptimizationParameters())
        finish = time()
        print(f'POT time: {finish - start}')
        start = time()

        logger.info('Get predictions on the validation set')
        predicted_validation_dataset = openvino_task.infer(validation_dataset.with_empty_annotations(),
                                                           InferenceParameters(is_evaluation=True))
        resultset = ResultSetEntity(model=optimized_model,
                                    ground_truth_dataset=validation_dataset,
                                    prediction_dataset=predicted_validation_dataset)
        logger.info('Performance of optimized model:')
        openvino_task.evaluate(resultset)
        logger.info(str(resultset.performance))
        finish = time()
        print(f'After POT validation: {finish - start}')


if __name__ == '__main__':
    sys.exit(main(parse_args()) or 0)
