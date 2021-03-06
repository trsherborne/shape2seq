"""
SEQ2SEQ IMAGE CAPTIONING
Tom Sherborne 8/5/18
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os, time, csv

import numpy as np
import tensorflow as tf

seq2seq = tf.contrib.seq2seq

from shapeworld import Dataset, tf_util
from shape2seq import CaptioningModel
from shape2seq import Config

FLAGS = tf.app.flags.FLAGS

tf.flags.DEFINE_string("data_dir", "", "Location of ShapeWorld data")
tf.flags.DEFINE_string("log_dir", "./models/final/sequence", "Directory location for logging")
tf.flags.DEFINE_string("dtype", "agreement", "Shapeworld Data Type")
tf.flags.DEFINE_string("name", "simple", "Shapeworld Data Name")
tf.flags.DEFINE_string("data_partition", "validation", "Which part of the dataset to test using")
tf.flags.DEFINE_string("parse_type", "", "shape, color or shape_color for input data formatting")
tf.flags.DEFINE_string("exp_tag", "", "Subfolder labelling under log_dir for this experiment")
tf.flags.DEFINE_integer("num_imgs", 1000, "How many images to test with")
tf.flags.DEFINE_string("decode_type", "greedy", "greedy/sample/beam")
tf.logging.set_verbosity(tf.logging.INFO)

def main(_):
    # FILESYSTEM SETUP ------------------------------------------------------------
    assert FLAGS.data_dir, "Must specify data location!"
    assert FLAGS.log_dir, "Must specify experiment to log to!"
    assert FLAGS.exp_tag, "Must specify experiment tag subfolder to log_dir %s" % FLAGS.log_dir
    assert FLAGS.parse_type
    
    # Folder setup for saving summaries and loading checkpoints
    save_root = FLAGS.log_dir + os.sep + FLAGS.exp_tag
    test_path = save_root + os.sep + "test"
    if not tf.gfile.IsDirectory(test_path):
        tf.gfile.MakeDirs(test_path)
    
    train_path = FLAGS.log_dir + os.sep + FLAGS.exp_tag + os.sep + "train"
    
    model_ckpt = tf.train.latest_checkpoint(train_path)  # Get checkpoint to load
    tf.logging.info("Loading checkpoint %s", model_ckpt)
    assert model_ckpt, "Checkpoints could not be loaded, check that train_path %s exists" % train_path
    
    # Sanity check graph reset
    tf.reset_default_graph()
    tf.logging.info("Clean graph reset...")
    
    # try:
    dataset = Dataset.create(dtype=FLAGS.dtype, name=FLAGS.name, config=FLAGS.data_dir)
    dataset.pixel_noise_stddev = 0.1
    dataset.random_sampling = False
    # except Exception:
    #     raise ValueError("config=%s did not point to a valid Shapeworld dataset" % FLAGS.data_dir)
    
    # Get parsing and parameter feats
    params = Config(mode="test", sw_specification=dataset.specification())
    
    # Parse decoding arg from CLI
    params.decode_type = FLAGS.decode_type
    assert params.decode_type in ['greedy', 'sample', 'beam']
    
    # MODEL SETUP ------------------------------------------------------------
    g = tf.Graph()
    with g.as_default():
        parser = SimpleBatchParser(src_vocab=dataset.vocabularies['language'], batch_type=FLAGS.parse_type)
        vocab, rev_vocab = parser.get_vocab()
        params.vocab_size = len(parser.tgt_vocab)
        
        batch = tf_util.batch_records(dataset, mode=FLAGS.data_partition, batch_size=params.batch_size)
        model = CaptioningModel(config=params, batch_parser=parser)
        model.build_model(batch)
        
        restore_model = tf.train.Saver()
        
        tf.logging.info("Network built...")
    
    # TESTING SETUP ------------------------------------------------------------
    
    if FLAGS.num_imgs < 1:
        num_imgs = params.instances_per_shard * params.num_shards
    else:
        num_imgs = FLAGS.num_imgs
    tf.logging.info("Running test for %d images", num_imgs)
    
    test_writer = tf.summary.FileWriter(logdir=test_path, graph=g)
    
    with tf.Session(graph=g, config=tf.ConfigProto(allow_soft_placement=True)) as sess:
        # Launch data loading queues
        coordinator = tf.train.Coordinator()
        queue_threads = tf.train.start_queue_runners(sess=sess, coord=coordinator)
        
        # Model restoration
        restore_model.restore(sess, model_ckpt)
        tf.logging.info("Model restored!")
        
        # Trained model does not need initialisation. Init the vocab conversation tables
        sess.run([tf.tables_initializer()])
        
        #  Freeze graph
        sess.graph.finalize()
        
        # Get global step
        global_step = tf.train.global_step(sess, model.global_step)
        tf.logging.info("Successfully loaded %s at global step = %d.",
                        os.path.basename(model_ckpt), global_step)
        
        start_test_time = time.time()
        corrects = []
        incorrects = []     # For correctly formed, but wrong captions
        misses = []         # For incorrectly formed captions
        perplexities = []
        
        for b_idx in range(num_imgs):
            # idx_batch = dataset.generate(n=params.batch_size, mode=FLAGS.data_partition, include_model=True)
            
            reference_caps, inf_decoder_outputs, batch_perplexity = sess.run(fetches=[model.reference_captions,
                                                                                      model.inf_decoder_output,
                                                                                      model.batch_perplexity],
                                                                             feed_dict={model.phase: 0})
            
            ref_cap = reference_caps.squeeze()
            inf_cap = inf_decoder_outputs.sample_id.squeeze()
            perplexities.append(batch_perplexity)

            if inf_cap.ndim > 0 and inf_cap.ndim > 0:
                print("%d REF -> %s | INF -> %s" %
                      (b_idx, " ".join(rev_vocab[r] for r in ref_cap), " ".join(rev_vocab[r] for r in inf_cap)))
    
                # Strip <S>, </S> and any irrelevant tokens and convert to list for order insensitivity
                ref_cap = set([tok for tok in ref_cap if int(tok) not in parser.token_filter])
                inf_cap = set([tok for tok in inf_cap if int(tok) not in parser.token_filter])
    
                if np.all([i in ref_cap for i in inf_cap]):
                    corrects.append(1)
                else:
                    incorrects.append((ref_cap, inf_cap))
            else:
                print("Skipping %d as inf_cap %s is malformed" % (b_idx, inf_cap))
                misses.append(1)
        
        # Overall scores for checkpoint
        avg_acc = np.mean(corrects).squeeze()
        std_acc = np.std(corrects).squeeze()
        print("Accuracy: %s -> %.5f ± %.5f | Misses: %d " % (FLAGS.parse_type, avg_acc, std_acc, len(misses)))

        avg_perplexity = np.mean(perplexities).squeeze()
        std_perplexity = np.std(perplexities).squeeze()
        print("------------")
        print("PERPLEXITY -> %.5f +- %.5f" % (avg_perplexity, std_perplexity))
        
        new_summ = tf.Summary()
        new_summ.value.add(tag="%s/avg_acc_%s" % (FLAGS.data_partition, FLAGS.name),
                           simple_value=avg_acc)

        new_summ.value.add(tag="%s/std_acc_%s" % (FLAGS.data_partition, FLAGS.name),
                           simple_value=std_acc)
        new_summ.value.add(tag="%s/perplexity_avg_%s" % (FLAGS.data_partition, FLAGS.name),
                           simple_value=avg_perplexity)
        new_summ.value.add(tag="%s/perplexity_std_%s" % (FLAGS.data_partition, FLAGS.name),
                           simple_value=std_perplexity)
        
        test_writer.add_summary(new_summ, tf.train.global_step(sess, model.global_step))
        test_writer.flush()
        
        coordinator.request_stop()
        coordinator.join(threads=queue_threads)

        end_time = time.time() - start_test_time
        tf.logging.info('Testing complete in %.2f-secs/%.2f-mins/%.2f-hours', end_time, end_time / 60,
                        end_time / (60 * 60))


if __name__ == "__main__":
    tf.app.run()

