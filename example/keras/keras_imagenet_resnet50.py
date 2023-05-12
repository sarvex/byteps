# Copyright 2019 Bytedance Inc. or its affiliates. All Rights Reserved.
# Copyright 2017 Uber Technologies, Inc. All Rights Reserved.
#
# ResNet-50 model training using Keras and BytePS.
#
# This model is an example of a computation-intensive model that achieves good accuracy on an image
# classification task.  It brings together distributed training concepts such as learning rate
# schedule adjustments with a warmup, randomized data reading, and checkpointing on the first worker
# only.
#
# Note: This model uses Keras native ImageDataGenerator and not the sophisticated preprocessing
# pipeline that is typically used to train state-of-the-art ResNet-50 model.  This results in ~0.5%
# increase in the top-1 validation error compared to the single-crop top-1 validation error from
# https://github.com/KaimingHe/deep-residual-networks.
#
from __future__ import print_function

import argparse
import tensorflow.keras as keras
from tensorflow.keras import backend as K
from tensorflow.keras.preprocessing import image
import tensorflow as tf
import byteps.keras as bps
import os

parser = argparse.ArgumentParser(description='Keras ImageNet Example',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--train-dir', default=os.path.expanduser('~/imagenet/train'),
                    help='path to training data')
parser.add_argument('--val-dir', default=os.path.expanduser('~/imagenet/validation'),
                    help='path to validation data')
parser.add_argument('--log-dir', default='./logs',
                    help='tensorboard log directory')
parser.add_argument('--checkpoint-format', default='./checkpoint-{epoch}.h5',
                    help='checkpoint file format')
parser.add_argument('--fp16-pushpull', action='store_true', default=False,
                    help='use fp16 compression during pushpull')

# Default settings from https://arxiv.org/abs/1706.02677.
parser.add_argument('--batch-size', type=int, default=32,
                    help='input batch size for training')
parser.add_argument('--val-batch-size', type=int, default=32,
                    help='input batch size for validation')
parser.add_argument('--epochs', type=int, default=90,
                    help='number of epochs to train')
parser.add_argument('--base-lr', type=float, default=0.0125,
                    help='learning rate for a single GPU')
parser.add_argument('--warmup-epochs', type=float, default=5,
                    help='number of warmup epochs')
parser.add_argument('--momentum', type=float, default=0.9,
                    help='SGD momentum')
parser.add_argument('--wd', type=float, default=0.00005,
                    help='weight decay')

args = parser.parse_args()

# initialize BytePS
bps.init()

# BytePS: pin GPU to be used to process local rank (one GPU per process)
config = tf.ConfigProto()
config.gpu_options.allow_growth = True
config.gpu_options.visible_device_list = str(bps.local_rank())
K.set_session(tf.Session(config=config))

resume_from_epoch = next(
    (
        try_epoch
        for try_epoch in range(args.epochs, 0, -1)
        if os.path.exists(args.checkpoint_format.format(epoch=try_epoch))
    ),
    0,
)
# BytePS: broadcast resume_from_epoch from rank 0 (which will have
# checkpoints) to other ranks.
resume_from_epoch = bps.broadcast(resume_from_epoch, 0, name='resume_from_epoch')

# BytePS: print logs on the first worker.
verbose = 1 if bps.rank() == 0 else 0

# Training data iterator.
train_gen = image.ImageDataGenerator(
    width_shift_range=0.33, height_shift_range=0.33, zoom_range=0.5, horizontal_flip=True,
    preprocessing_function=keras.applications.resnet50.preprocess_input)
train_iter = train_gen.flow_from_directory(args.train_dir,
                                           batch_size=args.batch_size,
                                           target_size=(224, 224))

# Validation data iterator.
test_gen = image.ImageDataGenerator(
    zoom_range=(0.875, 0.875), preprocessing_function=keras.applications.resnet50.preprocess_input)
test_iter = test_gen.flow_from_directory(args.val_dir,
                                         batch_size=args.val_batch_size,
                                         target_size=(224, 224))

# Set up standard ResNet-50 model.
model = keras.applications.resnet50.ResNet50(weights=None)

# BytePS: (optional) compression algorithm.
compression = bps.Compression.fp16 if args.fp16_pushpull else bps.Compression.none

# Restore from a previous checkpoint, if initial_epoch is specified.
# BytePS: restore on the first worker which will broadcast both model and optimizer weights
# to other workers.
if resume_from_epoch > 0 and bps.rank() == 0:
    model = bps.load_model(args.checkpoint_format.format(epoch=resume_from_epoch),
                           compression=compression)
else:
    # ResNet-50 model that is included with Keras is optimized for inference.
    # Add L2 weight decay & adjust BN settings.
    model_config = model.get_config()
    for layer, layer_config in zip(model.layers, model_config['layers']):
        if hasattr(layer, 'kernel_regularizer'):
            regularizer = keras.regularizers.l2(args.wd)
            layer_config['config']['kernel_regularizer'] = \
                {'class_name': regularizer.__class__.__name__,
                 'config': regularizer.get_config()}
        if type(layer) == keras.layers.BatchNormalization:
            layer_config['config']['momentum'] = 0.9
            layer_config['config']['epsilon'] = 1e-5

    model = keras.models.Model.from_config(model_config)

    # BytePS: adjust learning rate based on number of GPUs.
    opt = keras.optimizers.SGD(lr=args.base_lr * bps.size(),
                               momentum=args.momentum)

    # BytePS: add BytePS Distributed Optimizer.
    opt = bps.DistributedOptimizer(opt, compression=compression)

    model.compile(loss=keras.losses.categorical_crossentropy,
                  optimizer=opt,
                  metrics=['accuracy', 'top_k_categorical_accuracy'])

callbacks = [
    # BytePS: broadcast initial variable states from rank 0 to all other processes.
    # This is necessary to ensure consistent initialization of all workers when
    # training is started with random weights or restored from a checkpoint.
    bps.callbacks.BroadcastGlobalVariablesCallback(0),

    # BytePS: average metrics among workers at the end of every epoch.
    #
    # Note: This callback must be in the list before the ReduceLROnPlateau,
    # TensorBoard, or other metrics-based callbacks.
    bps.callbacks.MetricAverageCallback(),

    # BytePS: using `lr = 1.0 * bps.size()` from the very beginning leads to worse final
    # accuracy. Scale the learning rate `lr = 1.0` ---> `lr = 1.0 * bps.size()` during
    # the first five epochs. See https://arxiv.org/abs/1706.02677 for details.
    bps.callbacks.LearningRateWarmupCallback(warmup_epochs=args.warmup_epochs, verbose=verbose),

    # BytePS: after the warmup reduce learning rate by 10 on the 30th, 60th and 80th epochs.
    bps.callbacks.LearningRateScheduleCallback(start_epoch=args.warmup_epochs, end_epoch=30, multiplier=1.),
    bps.callbacks.LearningRateScheduleCallback(start_epoch=30, end_epoch=60, multiplier=1e-1),
    bps.callbacks.LearningRateScheduleCallback(start_epoch=60, end_epoch=80, multiplier=1e-2),
    bps.callbacks.LearningRateScheduleCallback(start_epoch=80, multiplier=1e-3),
]

# BytePS: save checkpoints only on the first worker to prevent other workers from corrupting them.
if bps.rank() == 0:
    callbacks.append(keras.callbacks.ModelCheckpoint(args.checkpoint_format))
    callbacks.append(keras.callbacks.TensorBoard(args.log_dir))

# Train the model. The training will randomly sample 1 / N batches of training data and
# 3 / N batches of validation data on every worker, where N is the number of workers.
# Over-sampling of validation data helps to increase probability that every validation
# example will be evaluated.
model.fit_generator(train_iter,
                    steps_per_epoch=len(train_iter) // bps.size(),
                    callbacks=callbacks,
                    epochs=args.epochs,
                    verbose=verbose,
                    workers=4,
                    initial_epoch=resume_from_epoch,
                    validation_data=test_iter,
                    validation_steps=3 * len(test_iter) // bps.size())

# Evaluate the model on the full data set.
score = bps.push_pull(model.evaluate_generator(test_iter, len(test_iter), workers=4))
if verbose:
    print('Test loss:', score[0])
    print('Test accuracy:', score[1])
