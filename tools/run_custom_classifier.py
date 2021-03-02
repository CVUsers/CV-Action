#!/usr/bin/env python
"""
Run a custom classifier that was obtained via the train_classifier script.

Usage:
  run_custom_classifier.py --custom_classifier=PATH
                           [--camera_id=CAMERA_ID]
                           [--path_in=FILENAME]
                           [--path_out=FILENAME]
                           [--title=TITLE]
                           [--use_gpu]
  run_custom_classifier.py (-h | --help)

Options:
  --custom_classifier=PATH   Path to the custom classifier to use
  --path_in=FILENAME         Video file to stream from
  --path_out=FILENAME        Video file to stream to
  --title=TITLE              This adds a title to the window display
"""
import os
import json
from docopt import docopt
import torch

import sense.display
from sense import feature_extractors
from sense.controller import Controller
from sense.downstream_tasks.nn_utils import LogisticRegression
from sense.downstream_tasks.nn_utils import Pipe
from sense.downstream_tasks.nn_utils import load_weights_from_resources
from sense.downstream_tasks.postprocess import PostprocessClassificationOutput


if __name__ == "__main__":
    # Parse arguments
    # args = docopt(__doc__)
    camera_id = 0
    path_in = None
    path_out = None
    custom_classifier = './sense_studio/data/'
    title = None
    use_gpu = True

    # Load original feature extractor
    feature_extractor = feature_extractors.StridedInflatedEfficientNet()
    feature_extractor.load_weights_from_resources('../resources/backbone/strided_inflated_efficientnet.ckpt')
    # feature_extractor = feature_extractors.StridedInflatedMobileNetV2()
    # feature_extractor.load_weights_from_resources(r'../resources\backbone\strided_inflated_mobilenet.ckpt')
    checkpoint = feature_extractor.state_dict()

    # Load custom classifier
    checkpoint_classifier = torch.load(os.path.join(custom_classifier, 'classifier.checkpoint'))
    # Update original weights in case some intermediate layers have been finetuned
    name_finetuned_layers = set(checkpoint.keys()).intersection(checkpoint_classifier.keys())
    for key in name_finetuned_layers:
        checkpoint[key] = checkpoint_classifier.pop(key)
    feature_extractor.load_state_dict(checkpoint)
    feature_extractor.eval()
    print('[debug] net:', feature_extractor)
    with open(os.path.join(custom_classifier, 'label2int.json')) as file:
        class2int = json.load(file)
    INT2LAB = {value: key for key, value in class2int.items()}

    gesture_classifier = LogisticRegression(num_in=feature_extractor.feature_dim,
                                            num_out=len(INT2LAB))
    gesture_classifier.load_state_dict(checkpoint_classifier)
    gesture_classifier.eval()
    print(gesture_classifier)

    # Concatenate feature extractor and met converter
    net = Pipe(feature_extractor, gesture_classifier)

    postprocessor = [
        PostprocessClassificationOutput(INT2LAB, smoothing=4)
    ]

    display_ops = [
        sense.display.DisplayFPS(expected_camera_fps=net.fps,
                                 expected_inference_fps=net.fps / net.step_size),
        sense.display.DisplayTopKClassificationOutputs(top_k=1, threshold=0.3),
    ]
    display_results = sense.display.DisplayResults(title=title, display_ops=display_ops)

    # Run live inference
    controller = Controller(
        neural_network=net,
        post_processors=postprocessor,
        results_display=display_results,
        callbacks=[],
        camera_id=camera_id,
        path_in=path_in,
        path_out=path_out,
        use_gpu=use_gpu
    )
    controller.run_inference()
