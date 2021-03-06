# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import deep_cnn
import input  # pylint: disable=redefined-builtin
import metrics
import os
import tensorflow as tf
import numpy as np
import pickle
import aggregation
from sklearn.decomposition import PCA, KernelPCA
import autodp
from autodp import rdp_bank,dp_acct, rdp_acct, privacy_calibrator
from sklearn.neighbors import NearestNeighbors
from sklearn.neighbors import KNeighborsClassifier
tf.flags.DEFINE_string('dataset', 'svhn', 'The name of the dataset to use')
tf.flags.DEFINE_integer('nb_labels', 10, 'Number of output classes')

tf.flags.DEFINE_string('data_dir','/tmp','Temporary storage')
tf.flags.DEFINE_string('train_dir','/tmp/train_dir',
                       'Where model ckpt are saved')
tf.flags.DEFINE_integer('gau_scale',40,'gaussian noise scale')
tf.flags.DEFINE_integer('max_steps', 3000, 'Number of training steps to run.')
tf.flags.DEFINE_integer('nb_teachers', 30, 'Teachers in the ensemble.')
tf.flags.DEFINE_integer('teacher_id', 0, 'ID of teacher being trained.')
tf.flags.DEFINE_integer('stdnt_share', 1000,
                        'Student share (last index) of the test data')
tf.flags.DEFINE_integer('extra', 0,'remove extra samples from training to test')
tf.flags.DEFINE_bool('pca', True, 'if true then apply pca as preprocessing')
tf.flags.DEFINE_bool('knn',1,'if 1 then replace dnn with knn')
tf.flags.DEFINE_bool('vat',False,'whether use vat to lable query, only use after vat')
tf.flags.DEFINE_boolean('deeper', False, 'Activate deeper CNN model')

FLAGS = tf.flags.FLAGS
prob = 0.2 # subsample probability for i
acct = rdp_acct.anaRDPacct()
delta = 1e-8
sigma = FLAGS.gau_scale #gaussian parameter
gaussian = lambda x: rdp_bank.RDP_gaussian({'sigma':sigma},x)

def convert_vat(test_data, test_labels,noisy_labels):

  log = {}
  log['labeled_train_images'] = test_data[:FLAGS.stdnt_share]
  log['labeled_train_labels'] = noisy_labels
  log['train_images'] = test_data[FLAGS.stdnt_share:-1000]
  log['train_labels'] = test_labels[FLAGS.stdnt_share:-1000]
  #use the remaining 1000 point for test
  log['test_images'] = test_data[:-1000]
  print('test_images.size',log['test_images'].shape)
  log['test_labels'] = test_labels[:-1000]
  file_vat = "../vat_tf/log/"+FLAGS.dataset+'_query='+str(FLAGS.stdnt_share)+'.pkl'
  with open(file_vat,'wb') as f:
    pickle.dump(log, f)

def pca(teacher, student):
  pca = PCA(n_components=60)
  pca.fit(teacher)
  max_component = pca.components_.T
  teacher = np.dot(teacher, max_component)
  student = np.dot(student, max_component)
  return teacher, student

def prepare_student_data(dataset, nb_teachers, save=False):
  """
  Takes a dataset name and the size of the teacher ensemble and prepares
f  training data for the student model, according to parameters indicated
  in flags above.
  :param dataset: string corresponding to mnist, cifar10, or svhn
  :param nb_teachers: number of teachers (in the ensemble) to learn from
  :param save: if set to True, will dump student training labels predicted by
               the ensemble of teachers (with Laplacian noise) as npy files.
               It also dumps the clean votes for each class (without noise) and
               the labels assigned by teachers
  :return: pairs of (data, labels) to be used for student training and testing
  """
  assert input.create_dir_if_needed(FLAGS.train_dir)

  # Load the dataset
  if dataset == 'svhn':
    train_data, train_labels,test_data, test_labels = input.ld_svhn(extended=True)
    train_data = np.reshape(train_data, [-1, 32 * 32*3])
    test_data = test_data.reshape([-1, 32 * 32*3])
  elif dataset == 'cifar10':
    train_data, train_labels, test_data, test_labels = input.ld_cifar10()
    train_data = np.reshape(train_data, [-1, 32 * 32*3])
    test_data = test_data.reshape([-1, 32 * 32*3])
  elif dataset == 'mnist':
    #test_data, test_labels = input.ld_mnist(test_only=True)
    train_data, train_labels, test_data, test_labels = input.ld_mnist()
    train_data = np.reshape(train_data, [-1, 28 * 28])
    test_data = test_data.reshape([-1, 28 * 28])
  else:
    print("Check value of dataset flag")
    return False

  # Make sure there is data leftover to be used as a test set
  """
    If FLAGS.extra >0, means we remove the first FLAGS.extra data point from 
  private dataset to student dataset. Default train_data is private.
  
    Ori_test_data records the original feature of test data, since we will apply 
    PCA later.
    
    iF FLAGS.vat == True, then '..ckpt-2000.py' is the prediction of student queries(A+B) from VAT, (A+B) is defined later

  """

  if FLAGS.extra >0:
    test_data = np.vstack((test_data, train_data[:FLAGS.extra]))
    test_labels = np.concatenate((test_labels,train_labels[:FLAGS.extra]))
    #print('test_label.shape',test_labels.shape)
    train_data = train_data[FLAGS.extra:]
    train_labels = train_labels[FLAGS.extra:]
  #print('train_size {} query_size {}'.format(train_data.shape[0], test_data.shape[0]))


  ori_test_data = test_data

  if FLAGS.vat == True and os.path.exists('record/svhn_model.ckpt-2000.npy'):
    vat_labels = np.load('record/svhn_model.ckpt-2000.npy')
    vat_labels = np.array(vat_labels, dtype=np.int32)
    print('vat_label.shape', vat_labels.shape)
    stdnt_test_data = ori_test_data[-1000:]
    stdnt_test_labels = test_labels[-1000:]
    return ori_test_data[:-1000], vat_labels, stdnt_test_data, stdnt_test_labels

  if FLAGS.pca == True:
    train_data, test_data = pca(train_data, test_data)

  stdnt_data = test_data[:FLAGS.stdnt_share]
  assert FLAGS.stdnt_share < len(test_data)

  """
    Compute teacher predictions for student queries
    There is a subsample scheme here, each query will subsample a prob*train_data for KNN, distance is based on Euclidean distance.
    autodp is used track privacy loss(compose_subsample_mechanisms)
    TO privately release every query, we add gaussian noise 
  """
  num_train = train_data.shape[0]
  teachers_preds = np.zeros([stdnt_data.shape[0],FLAGS.nb_teachers])

  for idx in range(len(stdnt_data)):
    if idx %100 ==0:
      print('idx=',idx)
    query_data = stdnt_data[idx]
    select_teacher = np.random.choice(train_data.shape[0],int(prob*num_train))
    dis = np.linalg.norm(train_data[select_teacher]-query_data, axis = 1)
    k_index = select_teacher[np.argsort(dis)[:FLAGS.nb_teachers]]
    teachers_preds[idx] = train_labels[k_index]
    acct.compose_poisson_subsampled_mechanisms(gaussian, prob, coeff=1)


  #compute privacy loss
  print("Composition of student  subsampled Gaussian mechanisms gives ", (acct.get_eps(delta), delta))
  teachers_preds = np.asarray(teachers_preds, dtype = np.int32)



  if not save:
    major_vote = aggregation.aggregation_knn(teachers_preds, sigma)
    stdnt_labels = major_vote
  else:
    # Request clean votes and clean labels as well
    stdnt_labels, clean_votes, labels_for_dump = aggregation.aggregation_knn(teachers_preds,sigma, return_clean_votes=True) #NOLINT(long-line)

    # Prepare filepath for numpy dump of clean votes
    filepath = FLAGS.data_dir + "/" + str(dataset) + '_' + str(nb_teachers) + '_student_clean_votes_gau_' + str(FLAGS.gau_scale) + '.npy'  # NOLINT(long-line)

    # Prepare filepath for numpy dump of clean labels
    filepath_labels = FLAGS.data_dir + "/" + str(dataset) + '_' + str(nb_teachers) + '_teachers_labels_gau_' + str(FLAGS.gau_scale) + '.npy'  # NOLINT(long-line)

    # Dump clean_votes array
    with tf.gfile.Open(filepath, mode='w') as file_obj:
      np.save(file_obj, clean_votes)

    # Dump labels_for_dump array
    with tf.gfile.Open(filepath_labels, mode='w') as file_obj:
      np.save(file_obj, labels_for_dump)


  ac_ag_labels = metrics.accuracy(stdnt_labels, test_labels[:FLAGS.stdnt_share])
  print("Accuracy of the aggregated labels: " + str(ac_ag_labels))

  """
  split  data point for semi-supervised training (VAT)
  Suppose  original test data is SVHN, then split it into 3 part A, B, C
  A has FLAGS.stdnt_share points, which are student queries answered by noisy KNN
  B has test_data[FLAGS.stdnt_share:-1000] data point, which is used as unlabeled feature for VAT
  C has the last 1k point for test
  if don't use VAT, then ignore convert_vat
  """
  convert_vat(ori_test_data,test_labels,stdnt_labels)

  stdnt_test_data = ori_test_data[-1000:]
  stdnt_test_labels = test_labels[-1000:]

  if save:
    # Prepare filepath for numpy dump of labels produced by noisy aggregation
    filepath = FLAGS.data_dir + "/" + str(dataset) + '_' + str(nb_teachers) + '_student_labels_lap_' + str(FLAGS.gau_scale) + '.npy' #NOLINT(long-line)

    # Dump student noisy labels array
    with tf.gfile.Open(filepath, mode='w') as file_obj:
      np.save(file_obj, stdnt_labels)

  return ori_test_data[:FLAGS.stdnt_share], stdnt_labels, stdnt_test_data, stdnt_test_labels


def train_student(dataset, nb_teachers):

  """
  This function trains a student using predictions made by an ensemble of
  teachers. The student and teacher models are trained using the same
  neural network architecture.
  :param dataset: string corresponding to mnist, cifar10, or svhn
  :param nb_teachers: number of teachers (in the ensemble) to learn from
  :return: True if student training went well
  """
  assert input.create_dir_if_needed(FLAGS.train_dir)

  # Call helper function to prepare student data using teacher predictions
  stdnt_dataset = prepare_student_data(dataset, nb_teachers, save=True)

  # Unpack the student dataset
  stdnt_data, stdnt_labels, stdnt_test_data, stdnt_test_labels = stdnt_dataset
  print('stdnt_test_data.shape',stdnt_test_data.shape)
  if dataset == 'cifar10':
    stdnt_data = stdnt_data.reshape([-1,32,32,3])
    stdnt_test_data = stdnt_test_data.reshape([-1,32,32,3])
  elif dataset == 'mnist':
    stdnt_data = stdnt_data.reshape([-1, 28,28,1])
    stdnt_test_data = stdnt_test_data.reshape([-1, 28,28,1])
  elif dataset == 'svhn':
    stdnt_data = stdnt_data.reshape([-1,32,32,3])
    stdnt_test_data = stdnt_test_data.reshape([-1, 32,32,3])
  # Prepare checkpoint filename and path
  if FLAGS.deeper:
    ckpt_path = FLAGS.train_dir + '/' + str(dataset) + '_' + str(nb_teachers) + '_student_deeper.ckpt' #NOLINT(long-line)
  else:
    ckpt_path = FLAGS.train_dir + '/' + str(dataset) + '_' + str(nb_teachers) + '_student.ckpt'  # NOLINT(long-line)

  # Start student training
  assert deep_cnn.train(stdnt_data, stdnt_labels, ckpt_path)

  # Compute final checkpoint name for student (with max number of steps)
  ckpt_path_final = ckpt_path + '-' + str(FLAGS.max_steps - 1)

  # Compute student label predictions on remaining chunk of test set
  student_preds = deep_cnn.softmax_preds(stdnt_test_data, ckpt_path_final)

  # Compute teacher accuracy
  precision = metrics.accuracy(student_preds, stdnt_test_labels)
  print('Precision of student after training: ' + str(precision))

  return True

def main(argv=None):

  train_student(FLAGS.dataset, FLAGS.nb_teachers)


if __name__ == '__main__':
  tf.app.run()
