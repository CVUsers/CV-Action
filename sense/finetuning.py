import glob
import itertools
import json
import matplotlib.pyplot as plt
import numpy as np
import os
import torch
import torch.nn as nn
import torch.optim as optim

from PIL import Image
from sense import camera
from sense import engine
from sklearn.metrics import confusion_matrix
from os.path import join

MODEL_TEMPORAL_DEPENDENCY = 45
MODEL_TEMPORAL_STRIDE = 4


def set_internal_padding_false(module):
    """
    This is used to turn off padding of steppable convolution layers.
    """
    if hasattr(module, "internal_padding"):
        module.internal_padding = False


class FeaturesDataset(torch.utils.data.Dataset):
    """ Features dataset.

    This object returns a list of  features from the features dataset based on the specified parameters.

    During training, only the number of timesteps required for one temporal output is sampled from the features.

    For training with non-temporal annotations, features extracted from padded segments are discarded
    so long as the minimum video length is met.

    For training with temporal annotations, samples from the background label and non-background label
    are returned with approximately the same probability.
    """

    def __init__(self, files, labels, temporal_annotation, full_network_minimum_frames,
                 num_timesteps=None, stride=4):
        self.files = files
        self.labels = labels
        self.num_timesteps = num_timesteps
        self.stride = stride
        self.temporal_annotations = temporal_annotation
        # Compute the number of features that come from padding:
        self.num_frames_padded = int((full_network_minimum_frames - 1) / self.stride)

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        features = np.load(self.files[idx])
        num_preds = features.shape[0]

        temporal_annotation = self.temporal_annotations[idx]
        # remove beggining of prediction that is not padded
        if temporal_annotation is not None:
            temporal_annotation = np.array(temporal_annotation)

        if self.num_timesteps and num_preds > self.num_timesteps:
            if temporal_annotation is not None:
                # creating the probability distribution based on temporal annotation
                prob0 = 1 / (2 * (np.sum(temporal_annotation == 0)))
                prob1 = 1 / (2 * (np.sum(temporal_annotation != 0)))
                probas = np.ones(len(temporal_annotation))
                probas[temporal_annotation == 0] = prob0
                probas[temporal_annotation != 0] = prob1
                probas = probas / np.sum(probas)

                # drawing the temporal label
                position = np.random.choice(len(temporal_annotation), 1, p=probas)[0]
                temporal_annotation = temporal_annotation[position:position + 1]

                # selecting the corresponding features.
                features = features[position * int(MODEL_TEMPORAL_STRIDE / self.stride): position * int(
                    MODEL_TEMPORAL_STRIDE / self.stride) + self.num_timesteps]
            else:
                # remove padded frames
                minimum_position = min(num_preds - self.num_timesteps - 1,
                                       self.num_frames_padded)
                minimum_position = max(minimum_position, 0)
                position = np.random.randint(minimum_position, num_preds - self.num_timesteps)
                features = features[position: position + self.num_timesteps]
            # will assume that we need only one output
        if temporal_annotation is None:
            temporal_annotation = [-100]
        return [features, self.labels[idx], temporal_annotation]


def generate_data_loader(dataset_dir, features_dir, tags_dir, label_names, label2int,
                         label2int_temporal_annotation, num_timesteps=5, batch_size=16, shuffle=True,
                         stride=4, path_annotations=None, temporal_annotation_only=False,
                         full_network_minimum_frames=MODEL_TEMPORAL_DEPENDENCY):
    # Find pre-computed features and derive corresponding labels
    tags_dir = os.path.join(dataset_dir, tags_dir)
    features_dir = os.path.join(dataset_dir, features_dir)
    labels_string = []
    temporal_annotation = []
    if not path_annotations:
        # Use all pre-computed features
        features = []
        labels = []
        for label in label_names:
            feature_temp = glob.glob(f'{features_dir}/{label}/*.npy')
            features += feature_temp
            labels += [label2int[label]] * len(feature_temp)
            labels_string += [label] * len(feature_temp)
    else:
        with open(path_annotations, 'r') as f:
            annotations = json.load(f)
        features = ['{}/{}/{}.npy'.format(features_dir, entry['label'],
                                          os.path.splitext(os.path.basename(entry['file']))[0])
                    for entry in annotations]
        labels = [label2int[entry['label']] for entry in annotations]
        labels_string = [entry['label'] for entry in annotations]

    # check if annotation exist for each video
    for label, feature in zip(labels_string, features):
        classe_mapping = {0: "counting_background",
                          1: f'{label}_position_1', 2:
                              f'{label}_position_2'}
        temporal_annotation_file = feature.replace(features_dir, tags_dir).replace(".npy", ".json")
        if os.path.isfile(temporal_annotation_file):
            annotation = json.load(open(temporal_annotation_file))["time_annotation"]
            annotation = np.array([label2int_temporal_annotation[classe_mapping[y]] for y in annotation])
            temporal_annotation.append(annotation)
        else:
            temporal_annotation.append(None)

    if temporal_annotation_only:
        features = [x for x, y in zip(features, temporal_annotation) if y is not None]
        labels = [x for x, y in zip(labels, temporal_annotation) if y is not None]
        temporal_annotation = [x for x in temporal_annotation if x is not None]

    # Build dataloader
    dataset = FeaturesDataset(features, labels, temporal_annotation,
                              num_timesteps=num_timesteps, stride=stride,
                              full_network_minimum_frames=full_network_minimum_frames)
    data_loader = torch.utils.data.DataLoader(dataset, shuffle=shuffle, batch_size=batch_size)

    return data_loader


def uniform_frame_sample(video, sample_rate):
    """
    Uniformly sample video frames according to the provided sample_rate.
    """

    depth = video.shape[0]
    if sample_rate < 1.:
        indices = np.arange(0, depth, 1. / sample_rate)
        offset = int((depth - indices[-1]) / 2)
        sampled_frames = (indices + offset).astype(np.int32)
        return video[sampled_frames]
    return video


def compute_features(video_path, path_out, inference_engine, num_timesteps=1, path_frames=None,
                     batch_size=None):
    video_source = camera.VideoSource(camera_id=None,
                                      size=inference_engine.expected_frame_size,
                                      filename=video_path)
    video_fps = video_source.get_fps()
    frames = []
    while True:
        images = video_source.get_image()
        if images is None:
            break
        else:
            image, image_rescaled = images
            frames.append(image_rescaled)
    frames = uniform_frame_sample(np.array(frames), inference_engine.fps / video_fps)

    # Compute how many frames are padded to the left in order to "warm up" the model -- removing previous predictions
    # from the internal states --  with the first image, and to ensure we have enough frames in the video.
    # We also want the first non padding frame to output a feature
    frames_to_add = MODEL_TEMPORAL_STRIDE * (MODEL_TEMPORAL_DEPENDENCY // MODEL_TEMPORAL_STRIDE + 1) - 1

    # Possible improvement : investigate if a symmetric or reflect padding could be better for
    # temporal annotation prediction instead of the static first frame
    frames = np.pad(frames, ((frames_to_add, 0), (0, 0), (0, 0), (0, 0)),
                    mode='edge')

    # Inference
    clip = frames[None].astype(np.float32)

    # Run the model on padded frames in order to remove the state in the current model comming
    # from the previous video.
    pre_features = inference_engine.infer(clip[:, 0:frames_to_add + 1], batch_size=batch_size)

    # Depending on the number of layers we finetune, we keep the number of features from padding
    # equal to the temporal dependancy of the model.
    temporal_dependancy_features = np.array(pre_features)[-num_timesteps:]

    # predictions of the actual video frames
    predictions = inference_engine.infer(clip[:, frames_to_add + 1:], batch_size=batch_size)
    predictions = np.concatenate([temporal_dependancy_features, predictions], axis=0)
    features = np.array(predictions)
    os.makedirs(os.path.dirname(path_out), exist_ok=True)
    np.save(path_out, features)

    if path_frames is not None:
        os.makedirs(os.path.dirname(path_frames), exist_ok=True)
        frames_to_save = []
        # remove the padded frames. extract frames starting at the first one (feature for the
        # first frame)
        for e, frame in enumerate(frames[frames_to_add:]):
            if e % MODEL_TEMPORAL_STRIDE == 0:
                frames_to_save.append(frame)

        for e, frame in enumerate(frames_to_save):
            Image.fromarray(frame[:, :, ::-1]).resize((400, 300)).save(
                os.path.join(path_frames, str(e) + '.jpg'), quality=50)


def compute_frames_features(inference_engine, split, label, dataset_path):
    # Get data-set from path, given split and label
    folder = join(dataset_path, f'videos_{split}', label)

    # Create features and frames folders for the given split and label
    features_folder = join(dataset_path, f'features_{split}', label)
    frames_folder = join(dataset_path, f'frames_{split}', label)
    os.makedirs(features_folder, exist_ok=True)
    os.makedirs(frames_folder, exist_ok=True)

    # Loop through all videos for the given class-label
    videos = glob.glob(folder + '/*.mp4')
    for e, video_path in enumerate(videos):
        print(f"\r  Class: \"{label}\"  -->  Processing video {e + 1} / {len(videos)}", end="")
        path_frames = join(frames_folder, os.path.basename(video_path).replace(".mp4", ""))
        path_features = join(features_folder, os.path.basename(video_path).replace(".mp4", ".npy"))
        if not os.path.isfile(path_features):
            os.makedirs(path_frames, exist_ok=True)
            compute_features(video_path, path_features, inference_engine,
                             num_timesteps=1, path_frames=path_frames, batch_size=64)


def extract_features(path_in, net, num_layers_finetune, use_gpu, num_timesteps=1):
    # Create inference engine
    inference_engine = engine.InferenceEngine(net, use_gpu=use_gpu)

    # extract features
    for dataset in ["train", "valid"]:
        videos_dir = os.path.join(path_in, f"videos_{dataset}")
        features_dir = os.path.join(path_in, f"features_{dataset}_num_layers_to_finetune={num_layers_finetune}")
        video_files = glob.glob(os.path.join(videos_dir, "*", "*.avi"))

        print(f"\nFound {len(video_files)} videos to process in the {dataset}set")

        for video_index, video_path in enumerate(video_files):
            print(f"\rExtract features from video {video_index + 1} / {len(video_files)}",
                  end="")
            path_out = video_path.replace(videos_dir, features_dir).replace(".mp4", ".npy")

            if os.path.isfile(path_out):
                print("\n\tSkipped - feature was already precomputed.")
            else:
                # Read all frames
                compute_features(video_path, path_out, inference_engine,
                                 num_timesteps=num_timesteps, path_frames=None, batch_size=16)

        print('\n')


def training_loops(net, train_loader, valid_loader, use_gpu, num_epochs, lr_schedule, label_names, path_out,
                   temporal_annotation_training=False):
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(net.parameters(), lr=0.0001)

    best_state_dict = None
    best_top1 = 0.
    best_loss = 9999

    for epoch in range(num_epochs):  # loop over the dataset multiple times
        new_lr = lr_schedule.get(epoch)
        if new_lr:
            print(f"update lr to {new_lr}")
            for param_group in optimizer.param_groups:
                param_group['lr'] = new_lr

        net.train()

        train_loss, train_top1, cnf_matrix = run_epoch(train_loader, net, criterion, optimizer,
                                                       use_gpu,
                                                       temporal_annotation_training=temporal_annotation_training)
        net.eval()
        valid_loss, valid_top1, cnf_matrix = run_epoch(valid_loader, net, criterion, None, use_gpu,
                                                       temporal_annotation_training=temporal_annotation_training)

        print('[%d] train loss: %.3f train top1: %.3f valid loss: %.3f top1: %.3f' % (epoch + 1, train_loss, train_top1,
                                                                                      valid_loss, valid_top1))

        if not temporal_annotation_training:
            if valid_top1 > best_top1:
                best_top1 = valid_top1
                best_state_dict = net.state_dict().copy()
                save_confusion_matrix(path_out, cnf_matrix, label_names)
        else:
            if valid_loss < best_loss:
                best_loss = valid_loss
                best_state_dict = net.state_dict().copy()

    print('Finished Training')
    return best_state_dict


def run_epoch(data_loader, net, criterion, optimizer=None, use_gpu=False,
              temporal_annotation_training=False):
    running_loss = 0.0
    epoch_top_predictions = []
    epoch_labels = []

    for i, data in enumerate(data_loader):
        # get the inputs; data is a list of [inputs, targets]
        inputs, targets, temporal_annotation = data
        if temporal_annotation_training:
            targets = temporal_annotation
        if use_gpu:
            inputs = inputs.cuda()
            targets = targets.cuda()

        # forward + backward + optimize
        if net.training:
            # Run on each batch element independently
            outputs = [net(input_i) for input_i in inputs]
            # Concatenate outputs to get a tensor of size batch_size x num_classes
            outputs = torch.cat(outputs, dim=0)

            if temporal_annotation_training:
                # take only targets one batch
                targets = targets[:, 0]
                # realign the number of outputs
                min_pred_number = min(outputs.shape[0], targets.shape[0])
                targets = targets[0:min_pred_number]
                outputs = outputs[0:min_pred_number]
        else:
            # Average predictions on the time dimension to get a tensor of size 1 x num_classes
            # This assumes validation operates with batch_size=1 and process all available features (no cropping)
            assert data_loader.batch_size == 1
            outputs = net(inputs[0])
            if temporal_annotation_training:
                targets = targets[0]
                # realign the number of outputs
                min_pred_number = min(outputs.shape[0], targets.shape[0])
                targets = targets[0:min_pred_number]
                outputs = outputs[0:min_pred_number]
            else:
                outputs = torch.mean(outputs, dim=0, keepdim=True)

        loss = criterion(outputs, targets)
        if optimizer is not None:
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

        # Store label and predictions to compute statistics later
        epoch_labels += list(targets.cpu().numpy())
        epoch_top_predictions += list(outputs.argmax(dim=1).cpu().numpy())

        # print statistics
        running_loss += loss.item()

    epoch_labels = np.array(epoch_labels)
    epoch_top_predictions = np.array(epoch_top_predictions)

    top1 = np.mean(epoch_labels == epoch_top_predictions)
    loss = running_loss / len(data_loader)

    cnf_matrix = confusion_matrix(epoch_labels, epoch_top_predictions)

    return loss, top1, cnf_matrix


def save_confusion_matrix(
        path_out,
        confusion_matrix_array,
        classes,
        normalize=False,
        title='Confusion matrix',
        cmap=plt.cm.Blues):
    """
    This function creates a matplotlib figure out of the provided confusion matrix and saves it
    to a file. The provided numpy array is also saved. Normalization can be applied by setting
    `normalize=True`.
    """

    plt.figure()
    plt.imshow(confusion_matrix_array, interpolation='nearest', cmap=cmap)
    plt.colorbar()
    tick_marks = np.arange(len(classes))
    plt.xticks(tick_marks, classes, rotation=90)
    plt.yticks(tick_marks, classes)

    accuracy = np.diag(confusion_matrix_array).sum() / confusion_matrix_array.sum()
    title += '\nAccuracy={:.1f}'.format(100 * float(accuracy))
    plt.title(title)

    if normalize:
        confusion_matrix_array = confusion_matrix_array.astype('float')
        confusion_matrix_array /= confusion_matrix_array.sum(axis=1)[:, np.newaxis]

    plt.ylabel('True label')
    plt.xlabel('Predicted label')
    thresh = confusion_matrix_array.max() / 2.
    for i, j in itertools.product(range(confusion_matrix_array.shape[0]),
                                  range(confusion_matrix_array.shape[1])):
        plt.text(j, i, confusion_matrix_array[i, j],
                 horizontalalignment="center",
                 color="white" if confusion_matrix_array[i, j] > thresh else "black")

    plt.savefig(os.path.join(path_out, 'confusion_matrix.png'), bbox_inches='tight',
                transparent=False, pad_inches=0.1, dpi=300)
    plt.close()

    np.save(os.path.join(path_out, 'confusion_matrix.npy'), confusion_matrix_array)
