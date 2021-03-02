#!/usr/bin/env python
"""
Real-time rep counter for jumping jacks and squats.

Usage:
  run_fitness_rep_counter.py [--camera_id=CAMERA_ID]
                             [--path_in=FILENAME]
                             [--path_out=FILENAME]
                             [--title=TITLE]
                             [--use_gpu]
  run_fitness_rep_counter.py (-h | --help)

Options:
  --path_in=FILENAME              Video file to stream from
  --path_out=FILENAME             Video file to stream to
  --title=TITLE                   This adds a title to the window display
"""
from docopt import docopt

import sense.display
from sense import feature_extractors
from sense.controller import Controller
from sense.downstream_tasks.fitness_rep_counting import INT2LAB
from sense.downstream_tasks.nn_utils import LogisticRegression
from sense.downstream_tasks.nn_utils import Pipe
from sense.downstream_tasks.nn_utils import load_weights_from_resources
from sense.downstream_tasks.postprocess import PostprocessClassificationOutput
from sense.downstream_tasks.postprocess import PostprocessRepCounts


if __name__ == "__main__":
    # Parse arguments
    args = docopt(__doc__)
    camera_id = int(args['--camera_id'] or 0)
    path_in = args['--path_in'] or None
    path_out = args['--path_out'] or None
    title = args['--title'] or None
    use_gpu = args['--use_gpu']

    # Load feature extractor
    feature_extractor = feature_extractors.StridedInflatedEfficientNet()
    feature_extractor.load_weights_from_resources('backbone/strided_inflated_efficientnet.ckpt')
    feature_extractor.eval()

    # Load a logistic regression classifier
    gesture_classifier = LogisticRegression(num_in=feature_extractor.feature_dim,
                                            num_out=5)
    checkpoint = load_weights_from_resources('fitness_rep_counting/efficientnet_logistic_regression.ckpt')
    gesture_classifier.load_state_dict(checkpoint)
    gesture_classifier.eval()

    # Concatenate feature extractor and met converter
    net = Pipe(feature_extractor, gesture_classifier)

    postprocessor = [
        PostprocessRepCounts(INT2LAB),
        PostprocessClassificationOutput(INT2LAB, smoothing=1)
    ]

    display_ops = [
        sense.display.DisplayFPS(expected_camera_fps=net.fps,
                                 expected_inference_fps=net.fps / net.step_size),
        sense.display.DisplayTopKClassificationOutputs(top_k=1, threshold=0.5),
        sense.display.DisplayRepCounts()
    ]
    display_results = sense.display.DisplayResults(title=title, display_ops=display_ops,
                                                   border_size=100)

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
