import unittest

import sense.downstream_tasks.nn_utils as nn_utils
from sense import RESOURCES_DIR


class TestLoadWeightsFromResources(unittest.TestCase):

    RELATIVE_PATH = 'gesture_detection/efficientnet_logistic_regression.ckpt'
    ABSOLUTE_PATH = '{}/{}'.format(RESOURCES_DIR, RELATIVE_PATH)

    def test_load_weights_from_resources_on_relative_path(self):
        _ = nn_utils.load_weights_from_resources(self.RELATIVE_PATH)

    def test_load_weights_from_resources_on_absolute_path(self):
        _ = nn_utils.load_weights_from_resources(self.ABSOLUTE_PATH)

    def test_load_weights_from_resources_on_wrong_path(self):
        wrong_path = 'this/path/does/not/exist'
        self.assertRaises(FileNotFoundError, nn_utils.load_weights_from_resources, wrong_path)
