"""Does object detection and segmentation on images."""
import json
import os
import math
import gc
import threading

import cv2
import numpy as np
import tensorflow as tf
from google.protobuf import text_format

import util_io
from object_detection.builders import image_resizer_builder
from object_detection.inference import detection_inference
from object_detection.inference import mask_inference
from object_detection.protos import image_resizer_pb2

tf.flags.DEFINE_string('input_images', None,
                       'A comma separated list of paths/patterns to input images.'
                       'e.g. "PATH/WITH/IMAGES/*,ANOTHER/PATH/1.jpg"')
tf.flags.DEFINE_string('output_path', None,
                       'Path to the output TFRecord.')
tf.flags.DEFINE_boolean('visualize_inference', False,
                        'If set, also outputs the annotated inference result image.')
tf.flags.DEFINE_boolean('output_cropped_image', False,
                        'If set, also outputs the cropped image to the output path. e.g. '
                        'OUTPUT_PATH/IMAGE_NAME_crop.png.')
tf.flags.DEFINE_boolean('only_output_cropped_single_object', False,
                        'Only used if FLAGS.output_cropped_image is True. Only outputs the cropped image if there is '
                        'one and only one object detected.')

tf.flags.DEFINE_string('inference_graph', None,
                       'Path to the inference graph with embedded weights.')
tf.flags.DEFINE_boolean('detect_masks', None,
                        'If true, output inferred masks.')
tf.flags.DEFINE_integer('override_num_detections', None,
                        'If set, this overrides the number of detections written in the graph.')
tf.flags.DEFINE_float('min_score_thresh', 0.25,
                      'Minimum score. Detection proposals below this score are discarded.')

FLAGS = tf.flags.FLAGS

get_writer_lock = threading.Lock()


def build_input():
  image_tensor = image_ph = tf.placeholder(dtype=tf.uint8, shape=[None, None, 3], name='image_ph')
  image_resizer_text_proto = """
    keep_aspect_ratio_resizer {
      min_dimension: 800
      max_dimension: 1365
    }
  """
  image_resizer_config = image_resizer_pb2.ImageResizer()
  text_format.Merge(image_resizer_text_proto, image_resizer_config)
  image_resizer_fn = image_resizer_builder.build(image_resizer_config)
  resized_image_tensor, _ = image_resizer_fn(image_tensor)
  resized_image_tensor = tf.cast(resized_image_tensor, dtype=tf.uint8)
  resized_image_tensor = tf.expand_dims(resized_image_tensor, 0)

  return image_ph, resized_image_tensor


def main(_):
  tf.logging.set_verbosity(tf.logging.INFO)
  inference_class = mask_inference if FLAGS.detect_masks else detection_inference
  if not os.path.exists(FLAGS.output_path):
    tf.gfile.MakeDirs(FLAGS.output_path)

  required_flags = ['input_images', 'output_path',
                    'inference_graph']
  for flag_name in required_flags:
    if not getattr(FLAGS, flag_name):
      raise ValueError('Flag --{} is required'.format(flag_name))

  config = tf.ConfigProto(allow_soft_placement=True)
  config.gpu_options.allow_growth = True
  sess = tf.Session(config=config)

  input_image_paths = []
  for v in FLAGS.input_images.split(','):
    if v:
      input_image_paths += tf.gfile.Glob(v)
  input_image_paths = sorted(input_image_paths)
  tf.logging.info('Reading input from %d files', len(input_image_paths))
  image_ph, image_tensor = build_input()

  tf.logging.info('Reading graph and building model...')
  detected_tensors = inference_class.build_inference_graph(
    image_tensor, FLAGS.inference_graph, override_num_detections=FLAGS.override_num_detections)

  tf.logging.info('Running inference and writing output to {}'.format(
    FLAGS.output_path))
  sess.run(tf.local_variables_initializer())
  batch_size = 10
  resolution = 256
  min_scaling_value = 3
  max_scaling_value = 6
  images_np = []
  paths = []
  images_index = 0
  skipped = 0

  for i, image_path in enumerate(input_image_paths):
    if i % batch_size == 0 and i != 0:
      for j, image_np in enumerate(images_np):
        try:
          result = inference_class.infer_detections(
            sess, image_tensor, detected_tensors,
            min_score_thresh=FLAGS.min_score_thresh,
            visualize_inference=FLAGS.visualize_inference,
            feed_dict={image_ph: image_np}
          )
        except:
          images_index += 1
          tf.logging.log_every_n(tf.logging.INFO, 'Processed %d/%d images...', 10, (j + (batch_size * ((i // batch_size) - 1))), len(input_image_paths) - skipped)  
          continue
        if FLAGS.output_cropped_image:
          if FLAGS.only_output_cropped_single_object and len(result["detection_score"]) == 1:
            num_outputs = 1
          else:
            num_outputs = len(result["detection_score"])

          for crop_i in range(num_outputs):
            if (result["detection_score"])[crop_i] > FLAGS.min_score_thresh and (result["detection_class_label"])[crop_i] == 1:
              base, ext = os.path.splitext(os.path.basename(paths[images_index]))
              output_crop = os.path.join(FLAGS.output_path, base + '_crop_%d.png' % crop_i)
              idims = image_np.shape  # np array with shape (height, width, num_color(1, 3, or 4))
              min_x = int(min(round(result["detection_bbox_xmin"][crop_i] * idims[1]), idims[1]))
              max_x = int(min(round(result["detection_bbox_xmax"][crop_i] * idims[1]), idims[1]))
              min_y = int(min(round(result["detection_bbox_ymin"][crop_i] * idims[0]), idims[0]))
              max_y = int(min(round(result["detection_bbox_ymax"][crop_i] * idims[0]), idims[0]))
              scaling = max(min_scaling_value, min(math.ceil(((idims[0] + idims[1]) / 2) / ((max_x - min_x + max_y - min_y) / 2)), max_scaling_value))
              range_x = abs(min_x - max_x)
              range_y = abs(min_y - max_y)
              mid_x = min_x + (range_x // 2)
              mid_y = min_y + (range_y // 2)
              max_dim = max(range_x, range_y) * scaling
              min_x = mid_x-(max_dim//2)
              max_x = mid_x+(max_dim//2)
              min_y = mid_y-(max_dim//2)
              max_y = mid_y+(max_dim//2)

              if (min_x < 0):
                max_x += abs(min_x)
                min_x = 0
              elif (min_y < 0):
                max_y += abs(min_y)
                min_y = 0
              if (max_x > idims[1]):
                min_x -= (max_x - idims[1])
                max_x = idims[1]
              elif (max_y > idims[0]):
                min_y -= (max_y - idims[0])
                max_y = idims[1]

              image_cropped = image_np[max(0, min_y):min(idims[0], max_y), max(0, min_x):min(idims[1], max_x), :]
              if image_cropped.shape[0] > resolution:
                image_cropped = cv2.resize(image_cropped, dsize=(resolution, resolution), interpolation=cv2.INTER_AREA)
                try:
                  util_io.imsave(output_crop, image_cropped)
                except:
                  continue
              else:
                continue
        if FLAGS.visualize_inference:
          output_image = os.path.join(FLAGS.output_path, os.path.basename(paths[images_index]))
          util_io.imsave(output_image, result['annotated_image'])
          del result['annotated_image']  # No need to write the image to json.
        if FLAGS.detect_masks:
          base, ext = os.path.splitext(os.path.basename(paths[images_index]))
          for mask_i in range(len(result['detected_masks'])):
            # Stores as png to preserve accurate mask values.
            output_mask = os.path.join(FLAGS.output_path, base + '_mask_%d' % mask_i + '.png')
            util_io.imsave(output_mask, np.array(result['detected_masks'][mask_i]) * 255)
          del result['detected_masks']  # Storing mask in json is pretty space consuming.

        # output_file = os.path.join(FLAGS.output_path, os.path.splitext(os.path.basename(paths[images_index]))[0] + '.json')
        # with open(output_file, 'w') as f:
        #   json.dump(result, f)
        images_index += 1
        tf.logging.log_every_n(tf.logging.INFO, 'Processed %d/%d images...', 10, (j + (batch_size * ((i // batch_size) - 1))), len(input_image_paths) - skipped)  
      images_np = []
      gc.collect()
    print("Loading %d / %d images from batch %d" % ((i % batch_size) + 1, batch_size, (i // batch_size) + 1))
    try: # sometimes images are truncated
      img = util_io.imread(image_path)
      images_np.append(img)
      paths.append(image_path)
    except:
      skipped += 1
  print('Finished processing all images in data set.')


if __name__ == '__main__':
  tf.app.run()
