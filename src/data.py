from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import random
import logging
import sys
import sklearn
import datetime
import numpy as np
import cv2

import mxnet as mx
from mxnet import ndarray as nd
#from . import _ndarray_internal as _internal
#from mxnet._ndarray_internal import _cvimresize as imresize
#from ._ndarray_internal import _cvcopyMakeBorder as copyMakeBorder
from mxnet import io
from mxnet import recordio
sys.path.append(os.path.join(os.path.dirname(__file__), 'common'))
import face_preprocess
import multiprocessing

logger = logging.getLogger()

def pick_triplets_impl(q_in, q_out):
  more = True
  while more:
      deq = q_in.get()
      if deq is None:
        more = False
      else:
        embeddings, emb_start_idx, nrof_images, alpha = deq
        print('running', emb_start_idx, nrof_images, os.getpid())
        for j in xrange(1,nrof_images):
            a_idx = emb_start_idx + j - 1
            neg_dists_sqr = np.sum(np.square(embeddings[a_idx] - embeddings), 1)
            for pair in xrange(j, nrof_images): # For every possible positive pair.
                p_idx = emb_start_idx + pair
                pos_dist_sqr = np.sum(np.square(embeddings[a_idx]-embeddings[p_idx]))
                neg_dists_sqr[emb_start_idx:emb_start_idx+nrof_images] = np.NaN
                all_neg = np.where(np.logical_and(neg_dists_sqr-pos_dist_sqr<alpha, pos_dist_sqr<neg_dists_sqr))[0]  # FaceNet selection
                #all_neg = np.where(neg_dists_sqr-pos_dist_sqr<alpha)[0] # VGG Face selecction
                nrof_random_negs = all_neg.shape[0]
                if nrof_random_negs>0:
                    rnd_idx = np.random.randint(nrof_random_negs)
                    n_idx = all_neg[rnd_idx]
                    #triplets.append( (a_idx, p_idx, n_idx) )
                    q_out.put( (a_idx, p_idx, n_idx) )
        #emb_start_idx += nrof_images
  print('exit',os.getpid())

class FaceImageIter(io.DataIter):

    def __init__(self, batch_size, data_shape,
                 path_imgrec = None,
                 shuffle=False, aug_list=None, mean = None,
                 rand_mirror = False,
                 ctx_num = 0, images_per_identity = 0, data_extra = None, hard_mining = False, 
                 triplet_params = None, coco_mode = False,
                 mx_model = None,
                 data_name='data', label_name='softmax_label', **kwargs):
        super(FaceImageIter, self).__init__()
        assert path_imgrec
        if path_imgrec:
            logging.info('loading recordio %s...',
                         path_imgrec)
            path_imgidx = path_imgrec[0:-4]+".idx"
            self.imgrec = recordio.MXIndexedRecordIO(path_imgidx, path_imgrec, 'r')  # pylint: disable=redefined-variable-type
            s = self.imgrec.read_idx(0)
            header, _ = recordio.unpack(s)
            if header.flag>0:
              print('header0 label', header.label)
              self.header0 = (int(header.label[0]), int(header.label[1]))
              #assert(header.flag==1)
              self.imgidx = range(1, int(header.label[0]))
              self.id2range = {}
              self.seq_identity = range(int(header.label[0]), int(header.label[1]))
              for identity in self.seq_identity:
                s = self.imgrec.read_idx(identity)
                header, _ = recordio.unpack(s)
                #print('flag', header.flag)
                #print(header.label)
                #assert(header.flag==2)
                self.id2range[identity] = (int(header.label[0]), int(header.label[1]))
              print('id2range', len(self.id2range))
            else:
              self.imgidx = list(self.imgrec.keys)
            if shuffle:
              self.seq = self.imgidx
              self.oseq = self.imgidx
            else:
              self.seq = None

        self.mean = mean
        self.nd_mean = None
        if self.mean:
          self.mean = np.array(self.mean, dtype=np.float32).reshape(1,1,3)
          self.nd_mean = mx.nd.array(self.mean).reshape((1,1,3))

        self.check_data_shape(data_shape)
        self.provide_data = [(data_name, (batch_size,) + data_shape)]
        self.batch_size = batch_size
        self.data_shape = data_shape
        self.shuffle = shuffle
        self.image_size = '%d,%d'%(data_shape[1],data_shape[2])
        self.rand_mirror = rand_mirror
        print('rand_mirror', rand_mirror)
        #self.cast_aug = mx.image.CastAug()
        #self.color_aug = mx.image.ColorJitterAug(0.4, 0.4, 0.4)
        self.ctx_num = ctx_num 
        self.per_batch_size = int(self.batch_size/self.ctx_num)
        self.images_per_identity = images_per_identity
        if self.images_per_identity>0:
          self.identities = int(self.per_batch_size/self.images_per_identity)
          self.per_identities = self.identities
          self.repeat = 3000000.0/(self.images_per_identity*len(self.id2range))
          self.repeat = int(self.repeat)
          print(self.images_per_identity, self.identities, self.repeat)
        self.data_extra = None
        if data_extra is not None:
          self.data_extra = nd.array(data_extra)
          self.provide_data = [(data_name, (batch_size,) + data_shape), ('extra', data_extra.shape)]
        self.hard_mining = hard_mining
        self.mx_model = mx_model
        if self.hard_mining:
          assert self.images_per_identity>0
          assert self.mx_model is not None
        self.triplet_params = triplet_params
        self.triplet_mode = False
        self.coco_mode = coco_mode
        if len(label_name)>0:
          self.provide_label = [(label_name, (batch_size,))]
        else:
          self.provide_label = []
        if self.coco_mode:
          assert self.triplet_params is None
          assert self.images_per_identity>0
        if self.triplet_params is not None:
          assert self.images_per_identity>0
          assert self.mx_model is not None
          self.triplet_bag_size = self.triplet_params[0]
          self.triplet_alpha = self.triplet_params[1]
          self.triplet_max_ap = self.triplet_params[2]
          assert self.triplet_bag_size>0
          assert self.triplet_alpha>=0.0
          assert self.triplet_alpha<=1.0
          self.triplet_mode = True
          self.triplet_oseq_cur = 0
          self.triplet_oseq_reset()
          self.seq_min_size = self.batch_size*2
        self.cur = 0
        self.is_init = False
        self.times = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        #self.reset()


    def ____pick_triplets(self, embeddings, nrof_images_per_class):
      emb_start_idx = 0
      people_per_batch = len(nrof_images_per_class)
      nrof_threads = 8
      q_in = multiprocessing.Queue()
      q_out = multiprocessing.Queue()
      processes = [multiprocessing.Process(target=pick_triplets_impl, args=(q_in, q_out)) \
                      for i in range(nrof_threads)]
      for p in processes:
          p.start()
      
      # VGG Face: Choosing good triplets is crucial and should strike a balance between
      #  selecting informative (i.e. challenging) examples and swamping training with examples that
      #  are too hard. This is achieve by extending each pair (a, p) to a triplet (a, p, n) by sampling
      #  the image n at random, but only between the ones that violate the triplet loss margin. The
      #  latter is a form of hard-negative mining, but it is not as aggressive (and much cheaper) than
      #  choosing the maximally violating example, as often done in structured output learning.

      for i in xrange(people_per_batch):
          nrof_images = int(nrof_images_per_class[i])
          job = (embeddings, emb_start_idx, nrof_images, self.triplet_alpha)
          emb_start_idx+=nrof_images
          q_in.put(job)
      for i in xrange(nrof_threads):
        q_in.put(None)
      print('joining')
      for p in processes:
          p.join()
      print('joined')
      q_out.put(None)

      triplets = []
      more = True
      while more:
        triplet = q_out.get()
        if triplet is None:
          more = False
        else:
          triplets.append(triplets)
      np.random.shuffle(triplets)
      return triplets

    #cal pairwise dists on single gpu
    def _pairwise_dists(self, embeddings):
      nd_embedding = mx.nd.array(embeddings, mx.gpu(0))
      pdists = []
      for idx in xrange(embeddings.shape[0]):
        a_embedding = nd_embedding[idx]
        body = mx.nd.broadcast_sub(a_embedding, nd_embedding)
        body = body*body
        body = mx.nd.sum_axis(body, axis=1)
        ret = body.asnumpy()
        #print(ret.shape)
        pdists.append(ret)
      return pdists

    def pairwise_dists(self, embeddings):
      nd_embedding_list = []
      for i in xrange(self.ctx_num):
        nd_embedding = mx.nd.array(embeddings, mx.gpu(i))
        nd_embedding_list.append(nd_embedding)
      nd_pdists = []
      pdists = []
      for idx in xrange(embeddings.shape[0]):
        emb_idx = idx%self.ctx_num
        nd_embedding = nd_embedding_list[emb_idx]
        a_embedding = nd_embedding[idx]
        body = mx.nd.broadcast_sub(a_embedding, nd_embedding)
        body = body*body
        body = mx.nd.sum_axis(body, axis=1)
        nd_pdists.append(body)
        if len(nd_pdists)==self.ctx_num or idx==embeddings.shape[0]-1:
          for x in nd_pdists:
            pdists.append(x.asnumpy())
          nd_pdists = []
      return pdists

    def pick_triplets(self, embeddings, nrof_images_per_class):
      emb_start_idx = 0
      triplets = []
      people_per_batch = len(nrof_images_per_class)
      #self.time_reset()
      pdists = self.pairwise_dists(embeddings)
      #self.times[3] += self.time_elapsed()

      for i in xrange(people_per_batch):
          nrof_images = int(nrof_images_per_class[i])
          for j in xrange(1,nrof_images):
              #self.time_reset()
              a_idx = emb_start_idx + j - 1
              #neg_dists_sqr = np.sum(np.square(embeddings[a_idx] - embeddings), 1)
              neg_dists_sqr = pdists[a_idx]
              #self.times[3] += self.time_elapsed()

              for pair in xrange(j, nrof_images): # For every possible positive pair.
                  p_idx = emb_start_idx + pair
                  #self.time_reset()
                  pos_dist_sqr = np.sum(np.square(embeddings[a_idx]-embeddings[p_idx]))
                  #self.times[4] += self.time_elapsed()
                  #self.time_reset()
                  neg_dists_sqr[emb_start_idx:emb_start_idx+nrof_images] = np.NaN
                  if self.triplet_max_ap>0.0:
                    if pos_dist_sqr>self.triplet_max_ap:
                      continue
                  all_neg = np.where(np.logical_and(neg_dists_sqr-pos_dist_sqr<self.triplet_alpha, pos_dist_sqr<neg_dists_sqr))[0]  # FaceNet selection
                  #self.times[5] += self.time_elapsed()
                  #self.time_reset()
                  #all_neg = np.where(neg_dists_sqr-pos_dist_sqr<alpha)[0] # VGG Face selecction
                  nrof_random_negs = all_neg.shape[0]
                  if nrof_random_negs>0:
                      rnd_idx = np.random.randint(nrof_random_negs)
                      n_idx = all_neg[rnd_idx]
                      triplets.append( (a_idx, p_idx, n_idx) )
          emb_start_idx += nrof_images
      np.random.shuffle(triplets)
      return triplets

    def __pick_triplets(self, embeddings, nrof_images_per_class):
      emb_start_idx = 0
      triplets = []
      people_per_batch = len(nrof_images_per_class)
      
      for i in xrange(people_per_batch):
          nrof_images = int(nrof_images_per_class[i])
          if nrof_images<2:
            continue
          for j in xrange(1,nrof_images):
              a_idx = emb_start_idx + j - 1
              pcount = nrof_images-1
              dists_a2all = np.sum(np.square(embeddings[a_idx] - embeddings), 1) #(N,)
              #print(a_idx, dists_a2all.shape)
              ba = emb_start_idx
              bb = emb_start_idx+nrof_images
              sorted_idx = np.argsort(dists_a2all)
              #print('assert', sorted_idx[0], a_idx)
              #assert sorted_idx[0]==a_idx
              #for idx in sorted_idx:
              #  print(idx, dists_a2all[idx])
              p2n_map = {}
              pfound = 0
              for idx in sorted_idx:
                if idx==a_idx: #is anchor
                  continue
                if idx<bb and idx>=ba: #is pos
                  p2n_map[idx] = [dists_a2all[idx], []] #ap, [neg_list]
                  pfound+=1
                else: # is neg
                  an = dists_a2all[idx]
                  if pfound==pcount and len(p2n_map)==0:
                    break
                  to_del = []
                  for p_idx in p2n_map:
                    v = p2n_map[p_idx]
                    an_ap = an - v[0]
                    if an_ap<self.triplet_alpha:
                      v[1].append(idx)
                    else:
                      #output
                      if len(v[1])>0:
                        n_idx = random.choice(v[1])
                        triplets.append( (a_idx, p_idx, n_idx) )
                      to_del.append(p_idx)
                  for _del in to_del:
                    del p2n_map[_del]
              for p_idx,v in p2n_map.iteritems():
                if len(v[1])>0:
                  n_idx = random.choice(v[1])
                  triplets.append( (a_idx, p_idx, n_idx) )
          emb_start_idx += nrof_images
      np.random.shuffle(triplets)
      return triplets

    def triplet_oseq_reset(self):
      #reset self.oseq by identities seq
      self.triplet_oseq_cur = 0
      ids = []
      for k in self.id2range:
        ids.append(k)
      random.shuffle(ids)
      self.oseq = []
      for _id in ids:
        v = self.id2range[_id]
        _list = range(*v)
        random.shuffle(_list)
        if len(_list)>self.images_per_identity:
          _list = _list[0:self.images_per_identity]
        self.oseq += _list
      print('oseq', len(self.oseq))

    def time_reset(self):
      self.time_now = datetime.datetime.now()

    def time_elapsed(self):
      time_now = datetime.datetime.now()
      diff = time_now - self.time_now
      return diff.total_seconds()


    def select_triplets(self):
      self.seq = []
      while len(self.seq)<self.seq_min_size:
        self.time_reset()
        embeddings = None
        bag_size = self.triplet_bag_size
        batch_size = self.batch_size
        #data = np.zeros( (bag_size,)+self.data_shape )
        #label = np.zeros( (bag_size,) )
        tag = []
        #idx = np.zeros( (bag_size,) )
        print('eval %d images..'%bag_size, self.triplet_oseq_cur)
        print('triplet time stat', self.times)
        if self.triplet_oseq_cur+bag_size>len(self.oseq):
          self.triplet_oseq_reset()
          print('eval %d images..'%bag_size, self.triplet_oseq_cur)
        self.times[0] += self.time_elapsed()
        self.time_reset()
        #print(data.shape)
        data = nd.zeros( self.provide_data[0][1] )
        label = nd.zeros( self.provide_label[0][1] )
        ba = 0
        while True:
          bb = min(ba+batch_size, bag_size)
          if ba>=bb:
            break
          #_batch = self.data_iter.next()
          #_data = _batch.data[0].asnumpy()
          #print(_data.shape)
          #_label = _batch.label[0].asnumpy()
          #data[ba:bb,:,:,:] = _data
          #label[ba:bb] = _label
          for i in xrange(ba, bb):
            _idx = self.oseq[i+self.triplet_oseq_cur]
            s = self.imgrec.read_idx(_idx)
            header, img = recordio.unpack(s)
            img = self.imdecode(img)
            data[i-ba][:] = self.postprocess_data(img)
            label[i-ba][:] = header.label
            tag.append( ( int(header.label), _idx) )
            #idx[i] = _idx

          db = mx.io.DataBatch(data=(data,), label=(label,))
          self.mx_model.forward(db, is_train=False)
          net_out = self.mx_model.get_outputs()
          #print('eval for selecting triplets',ba,bb)
          #print(net_out)
          #print(len(net_out))
          #print(net_out[0].asnumpy())
          net_out = net_out[0].asnumpy()
          #print(net_out)
          #print('net_out', net_out.shape)
          if embeddings is None:
            embeddings = np.zeros( (bag_size, net_out.shape[1]))
          embeddings[ba:bb,:] = net_out
          ba = bb
        assert len(tag)==bag_size
        self.triplet_oseq_cur+=bag_size
        embeddings = sklearn.preprocessing.normalize(embeddings)
        self.times[1] += self.time_elapsed()
        self.time_reset()
        nrof_images_per_class = [1]
        for i in xrange(1, bag_size):
          if tag[i][0]==tag[i-1][0]:
            nrof_images_per_class[-1]+=1
          else:
            nrof_images_per_class.append(1)
          
        triplets = self.pick_triplets(embeddings, nrof_images_per_class) # shape=(T,3)
        print('found triplets', len(triplets))
        ba = 0
        while True:
          bb = ba+self.per_batch_size//3
          if bb>len(triplets):
            break
          _triplets = triplets[ba:bb]
          for i in xrange(3):
            for triplet in _triplets:
              _pos = triplet[i]
              _idx = tag[_pos][1]
              self.seq.append(_idx)
          ba = bb
        self.times[2] += self.time_elapsed()

    def triplet_reset(self):
      self.select_triplets()

    def hard_mining_reset(self):
      #import faiss
      from annoy import AnnoyIndex
      data = nd.zeros( self.provide_data[0][1] )
      label = nd.zeros( self.provide_label[0][1] )
      #label = np.zeros( self.provide_label[0][1] )
      X = None
      ba = 0
      batch_num = 0
      while ba<len(self.oseq):
        batch_num+=1
        if batch_num%10==0:
          print('loading batch',batch_num, ba)
        bb = min(ba+self.batch_size, len(self.oseq))
        _count = bb-ba
        for i in xrange(_count):
          idx = self.oseq[i+ba]
          s = self.imgrec.read_idx(idx)
          header, img = recordio.unpack(s)
          img = self.imdecode(img)
          data[i][:] = self.postprocess_data(img)
          label[i][:] = header.label
        db = mx.io.DataBatch(data=(data,self.data_extra), label=(label,))
        self.mx_model.forward(db, is_train=False)
        net_out = self.mx_model.get_outputs()
        embedding = net_out[0].asnumpy()
        nembedding = sklearn.preprocessing.normalize(embedding)
        if _count<self.batch_size:
          nembedding = nembedding[0:_count,:]
        if X is None:
          X = np.zeros( (len(self.id2range), nembedding.shape[1]), dtype=np.float32 )
        nplabel = label.asnumpy()
        for i in xrange(_count):
          ilabel = int(nplabel[i])
          #print(ilabel, ilabel.__class__)
          X[ilabel] += nembedding[i]
        ba = bb
      X = sklearn.preprocessing.normalize(X)
      d = X.shape[1]
      t = AnnoyIndex(d, metric='euclidean')
      for i in xrange(X.shape[0]):
        t.add_item(i, X[i])
      print('start to build index')
      t.build(20)
      print(X.shape)
      k = self.per_identities
      self.seq = []
      for i in xrange(X.shape[0]):
        nnlist = t.get_nns_by_item(i, k)
        assert nnlist[0]==i
        for _label in nnlist:
          assert _label<len(self.id2range)
          _id = self.header0[0]+_label
          v = self.id2range[_id]
          _list = range(*v)
          if len(_list)<self.images_per_identity:
            random.shuffle(_list)
          else:
            _list = np.random.choice(_list, self.images_per_identity, replace=False)
          for i in xrange(self.images_per_identity):
            _idx = _list[i%len(_list)]
            self.seq.append(_idx)
      #faiss_params = [20,5]
      #quantizer = faiss.IndexFlatL2(d)  # the other index
      #index = faiss.IndexIVFFlat(quantizer, d, faiss_params[0], faiss.METRIC_L2)
      #assert not index.is_trained
      #index.train(X)
      #index.add(X)
      #assert index.is_trained
      #print('trained')
      #index.nprobe = faiss_params[1]
      #D, I = index.search(X, k)     # actual search
      #print(I.shape)
      #self.seq = []
      #for i in xrange(I.shape[0]):
      #  #assert I[i][0]==i
      #  for j in xrange(k):
      #    _label = I[i][j]
      #    assert _label<len(self.id2range)
      #    _id = self.header0[0]+_label
      #    v = self.id2range[_id]
      #    _list = range(*v)
      #    if len(_list)<self.images_per_identity:
      #      random.shuffle(_list)
      #    else:
      #      _list = np.random.choice(_list, self.images_per_identity, replace=False)
      #    for i in xrange(self.images_per_identity):
      #      _idx = _list[i%len(_list)]
      #      self.seq.append(_idx)

    def reset(self):
        """Resets the iterator to the beginning of the data."""
        print('call reset()')
        self.cur = 0
        if self.images_per_identity>0:
          if self.triplet_mode:
            self.triplet_reset()
          elif not self.hard_mining:
            self.seq = []
            idlist = []
            for _id,v in self.id2range.iteritems():
              idlist.append((_id,range(*v)))
            for r in xrange(self.repeat):
              if r%10==0:
                print('repeat', r)
              if self.shuffle:
                random.shuffle(idlist)
              for item in idlist:
                _id = item[0]
                _list = item[1]
                #random.shuffle(_list)
                if len(_list)<self.images_per_identity:
                  random.shuffle(_list)
                else:
                  _list = np.random.choice(_list, self.images_per_identity, replace=False)
                for i in xrange(self.images_per_identity):
                  _idx = _list[i%len(_list)]
                  self.seq.append(_idx)
          else:
            self.hard_mining_reset()
          print('seq len', len(self.seq))
        else:
          if self.shuffle:
              random.shuffle(self.seq)
        if self.seq is None and self.imgrec is not None:
            self.imgrec.reset()

    def num_samples(self):
      return len(self.seq)

    def next_sample(self):
        """Helper function for reading in next sample."""
        #set total batch size, for example, 1800, and maximum size for each people, for example 45
        if self.seq is not None:
          if self.cur >= len(self.seq):
              raise StopIteration
          idx = self.seq[self.cur]
          self.cur += 1
          if self.imgrec is not None:
            s = self.imgrec.read_idx(idx)
            header, img = recordio.unpack(s)
            return header.label, img, None, None
          else:
            label, fname, bbox, landmark = self.imglist[idx]
            return label, self.read_image(fname), bbox, landmark
        else:
            s = self.imgrec.read()
            if s is None:
                raise StopIteration
            header, img = recordio.unpack(s)
            return header.label, img, None, None

    def brightness_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      src *= alpha
      return src

    def contrast_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      coef = np.array([[[0.299, 0.587, 0.114]]])
      gray = src * coef
      gray = (3.0 * (1.0 - alpha) / gray.size) * np.sum(gray)
      src *= alpha
      src += gray
      return src

    def saturation_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      coef = np.array([[[0.299, 0.587, 0.114]]])
      gray = src * coef
      gray = np.sum(gray, axis=2, keepdims=True)
      gray *= (1.0 - alpha)
      src *= alpha
      src += gray
      return src

    def color_aug(self, img, x):
      augs = [self.brightness_aug, self.contrast_aug, self.saturation_aug]
      random.shuffle(augs)
      for aug in augs:
        #print(img.shape)
        img = aug(img, x)
        #print(img.shape)
      return img

    def mirror_aug(self, img):
      _rd = random.randint(0,1)
      if _rd==1:
        for c in xrange(img.shape[2]):
          img[:,:,c] = np.fliplr(img[:,:,c])
      return img


    def next(self):
        if not self.is_init:
          self.reset()
          self.is_init = True
        """Returns the next batch of data."""
        #print('in next', self.cur, self.labelcur)
        batch_size = self.batch_size
        c, h, w = self.data_shape
        batch_data = nd.empty((batch_size, c, h, w))
        if self.provide_label is not None:
          batch_label = nd.empty(self.provide_label[0][1])
        i = 0
        try:
            while i < batch_size:
                label, s, bbox, landmark = self.next_sample()
                _data = self.imdecode(s)
                if self.rand_mirror:
                  _rd = random.randint(0,1)
                  if _rd==1:
                    _data = mx.ndarray.flip(data=_data, axis=1)
                if self.nd_mean is not None:
                    _data = _data.astype('float32')
                    _data -= self.nd_mean
                    _data *= 0.0078125
                #_npdata = _data.asnumpy()
                #if landmark is not None:
                #  _npdata = face_preprocess.preprocess(_npdata, bbox = bbox, landmark=landmark, image_size=self.image_size)
                #if self.rand_mirror:
                #  _npdata = self.mirror_aug(_npdata)
                #if self.mean is not None:
                #  _npdata = _npdata.astype(np.float32)
                #  _npdata -= self.mean
                #  _npdata *= 0.0078125
                #nimg = np.zeros(_npdata.shape, dtype=np.float32)
                #nimg[self.patch[1]:self.patch[3],self.patch[0]:self.patch[2],:] = _npdata[self.patch[1]:self.patch[3], self.patch[0]:self.patch[2], :]
                #_data = mx.nd.array(nimg)
                data = [_data]
                try:
                    self.check_valid_image(data)
                except RuntimeError as e:
                    logging.debug('Invalid image, skipping:  %s', str(e))
                    continue
                #print('aa',data[0].shape)
                #data = self.augmentation_transform(data)
                #print('bb',data[0].shape)
                for datum in data:
                    assert i < batch_size, 'Batch size must be multiples of augmenter output length'
                    #print(datum.shape)
                    batch_data[i][:] = self.postprocess_data(datum)
                    if self.provide_label is not None:
                      if not self.coco_mode:
                        batch_label[i][:] = label
                      else:
                        batch_label[i][:] = (i%self.per_batch_size)//self.images_per_identity
                    i += 1
        except StopIteration:
            if i<batch_size:
                raise StopIteration

        #print('next end', batch_size, i)
        _label = None
        if self.provide_label is not None:
          _label = [batch_label]
        if self.data_extra is not None:
          return io.DataBatch([batch_data, self.data_extra], _label, batch_size - i)
        else:
          return io.DataBatch([batch_data], _label, batch_size - i)

    def check_data_shape(self, data_shape):
        """Checks if the input data shape is valid"""
        if not len(data_shape) == 3:
            raise ValueError('data_shape should have length 3, with dimensions CxHxW')
        if not data_shape[0] == 3:
            raise ValueError('This iterator expects inputs to have 3 channels.')

    def check_valid_image(self, data):
        """Checks if the input data is valid"""
        if len(data[0].shape) == 0:
            raise RuntimeError('Data shape is wrong')

    def imdecode(self, s):
        """Decodes a string or byte string to an NDArray.
        See mx.img.imdecode for more details."""
        img = mx.image.imdecode(s) #mx.ndarray
        return img

    def read_image(self, fname):
        """Reads an input image `fname` and returns the decoded raw bytes.

        Example usage:
        ----------
        >>> dataIter.read_image('Face.jpg') # returns decoded raw bytes.
        """
        with open(os.path.join(self.path_root, fname), 'rb') as fin:
            img = fin.read()
        return img

    def augmentation_transform(self, data):
        """Transforms input data with specified augmentation."""
        for aug in self.auglist:
            data = [ret for src in data for ret in aug(src)]
        return data

    def postprocess_data(self, datum):
        """Final postprocessing step before image is loaded into the batch."""
        return nd.transpose(datum, axes=(2, 0, 1))

class FaceImageIterList(io.DataIter):
  def __init__(self, iter_list):
    assert len(iter_list)>0
    self.provide_data = iter_list[0].provide_data
    self.provide_label = iter_list[0].provide_label
    self.iter_list = iter_list
    self.cur_iter = None

  def reset(self):
    self.cur_iter.reset()

  def next(self):
    self.cur_iter = random.choice(self.iter_list)
    while True:
      try:
        ret = self.cur_iter.next()
      except StopIteration:
        self.cur_iter.reset()
        continue
      return ret

class FaceIter(mx.io.DataIter):
  def __init__(self, data_shape, path_imglist, mod, ctx_num, batch_size=90, bag_size=1800, images_per_person=40, alpha = 0.2, data_name='data', label_name='softmax_label'):
    assert batch_size%ctx_num==0
    assert (batch_size//ctx_num)%3==0
    assert bag_size%batch_size==0
    self.mod = mod
    self.ctx_num = ctx_num
    self.batch_size = batch_size
    #self.batch_size_per_epoch = batch_size_per_epoch
    self.bag_size = bag_size
    self.data_shape = data_shape
    self.alpha = alpha
    self.data_name = data_name
    self.label_name = label_name
    #print(source_iter.provide_data)
    self.provide_data = [(self.data_name, (self.batch_size,) + self.data_shape)]
    self.provide_label = [(self.label_name, (self.batch_size,) )]
    #self.buffer = []
    #self.buffer_index = 0
    self.triplet_index = 0
    self.triplets = []
    self.data_iter = FaceImageIter(batch_size = self.batch_size, data_shape = data_shape, 
        images_per_person = images_per_person, margin = 44, 
        path_imglist = path_imglist, shuffle=True, 
        resize=182, rand_crop=True, rand_mirror=True)


  def pick_triplets(self, embeddings, nrof_images_per_class):
    trip_idx = 0
    emb_start_idx = 0
    num_trips = 0
    triplets = []
    people_per_batch = len(nrof_images_per_class)
    
    # VGG Face: Choosing good triplets is crucial and should strike a balance between
    #  selecting informative (i.e. challenging) examples and swamping training with examples that
    #  are too hard. This is achieve by extending each pair (a, p) to a triplet (a, p, n) by sampling
    #  the image n at random, but only between the ones that violate the triplet loss margin. The
    #  latter is a form of hard-negative mining, but it is not as aggressive (and much cheaper) than
    #  choosing the maximally violating example, as often done in structured output learning.

    for i in xrange(people_per_batch):
        nrof_images = int(nrof_images_per_class[i])
        for j in xrange(1,nrof_images):
            a_idx = emb_start_idx + j - 1
            neg_dists_sqr = np.sum(np.square(embeddings[a_idx] - embeddings), 1)
            for pair in xrange(j, nrof_images): # For every possible positive pair.
                p_idx = emb_start_idx + pair
                pos_dist_sqr = np.sum(np.square(embeddings[a_idx]-embeddings[p_idx]))
                neg_dists_sqr[emb_start_idx:emb_start_idx+nrof_images] = np.NaN
                all_neg = np.where(np.logical_and(neg_dists_sqr-pos_dist_sqr<self.alpha, pos_dist_sqr<neg_dists_sqr))[0]  # FaceNet selection
                #all_neg = np.where(neg_dists_sqr-pos_dist_sqr<alpha)[0] # VGG Face selecction
                nrof_random_negs = all_neg.shape[0]
                if nrof_random_negs>0:
                    rnd_idx = np.random.randint(nrof_random_negs)
                    n_idx = all_neg[rnd_idx]
                    #triplets.append((image_paths[a_idx], image_paths[p_idx], image_paths[n_idx]))
                    triplets.append( (a_idx, p_idx, n_idx) )
                    #triplets.append((image_paths[a_idx], image_paths[p_idx], image_paths[n_idx]))
                    #print('Triplet %d: (%d, %d, %d), pos_dist=%2.6f, neg_dist=%2.6f (%d, %d, %d, %d, %d)' % 
                    #    (trip_idx, a_idx, p_idx, n_idx, pos_dist_sqr, neg_dists_sqr[n_idx], nrof_random_negs, rnd_idx, i, j, emb_start_idx))
                    trip_idx += 1

                num_trips += 1

        emb_start_idx += nrof_images

    np.random.shuffle(triplets)
    return triplets
    #return triplets, num_trips, len(triplets)

  def select_triplets(self):
    self.triplet_index = 0
    self.triplets = []
    embeddings = None
    ba = 0
    bag_size = self.bag_size
    batch_size = self.batch_size
    data = np.zeros( (bag_size,)+self.data_shape )
    label = np.zeros( (bag_size,) )
    print('eval %d images..'%bag_size)
    #print(data.shape)
    while ba<bag_size:
      bb = ba+batch_size
      _batch = self.data_iter.next()
      _data = _batch.data[0].asnumpy()
      #print(_data.shape)
      _label = _batch.label[0].asnumpy()
      data[ba:bb,:,:,:] = _data
      label[ba:bb] = _label

      self.mod.forward(_batch, is_train=False)
      net_out = self.mod.get_outputs()
      #print('eval for selecting triplets',ba,bb)
      #print(net_out)
      #print(len(net_out))
      #print(net_out[0].asnumpy())
      net_out = net_out[0].asnumpy()
      #print(net_out)
      #print('net_out', net_out.shape)
      if embeddings is None:
        embeddings = np.zeros( (bag_size, net_out.shape[1]))
      embeddings[ba:bb,:] = net_out
      ba = bb
    nrof_images_per_class = [1]
    for i in xrange(1, bag_size):
      if label[i]==label[i-1]:
        nrof_images_per_class[-1]+=1
      else:
        nrof_images_per_class.append(1)
      
    self.triplets = self.pick_triplets(embeddings, nrof_images_per_class) # shape=(T,3)
    self.buffer_data = data
    self.buffer_label = label
    self.embeddings = embeddings
    print('buffering triplets..', len(self.triplets))
    print('epoches...', len(self.triplets)*3//self.batch_size)
    if len(self.triplets)==0:
      print(embeddings.shape, label.shape, data.shape, ba)
      print('images_per_class', nrof_images_per_class)
      print(label)
      print(embeddings)
      sys.exit(0)


  def next(self):
    batch_size = self.batch_size
    ta = self.triplet_index
    tb = ta + batch_size//3
    while tb>=len(self.triplets):
      self.select_triplets()
      ta = self.triplet_index
      tb = ta + batch_size//3
    data = np.zeros( (batch_size,)+self.data_shape )
    label = np.zeros( (batch_size,) )
    for ti in xrange(ta, tb):
      triplet = self.triplets[ti]
      anchor = self.embeddings[triplet[0]]
      positive = self.embeddings[triplet[1]]
      negative = self.embeddings[triplet[2]]
      ap = anchor-positive
      ap = ap*ap
      ap = np.sum(ap)
      an = anchor-negative
      an = an*an
      an = np.sum(an)
      assert ap<=an
      assert ap+self.alpha>=an
      _ti = ti-ta
      ctx_block = (_ti*3)//(self.batch_size//self.ctx_num)
      #apn_block = ((ti*3)%self.batch_size)%3
      #apn_pos = ((ti*3)%self.batch_size)//3
      base_pos = ctx_block*(self.batch_size//self.ctx_num) + (_ti%(self.batch_size//self.ctx_num//3)) 
      for ii in xrange(3):
        id = triplet[ii]
        pos = base_pos + ii*(self.batch_size//self.ctx_num//3)
        #print('id-pos', _ti, ii, pos)
        data[pos,:,:,:] = self.buffer_data[id, :,:,:]
        label[pos] = self.buffer_label[id]
    db = io.DataBatch(data=(nd.array(data),), label=(nd.array(label),))
    self.triplet_index = tb
    return db


  def reset(self):
    self.data_iter.reset()
    self.triplet_index = 0
    self.triplets = []
    #self.target_iter.reset()

class FaceImageIter2(io.DataIter):

    def __init__(self, batch_size, data_shape, path_imglist=None, path_root=None,
                 path_imgrec = None,
                 shuffle=False, aug_list=None, exclude_lfw = False, mean = None,
                 patch = [0,0,96,112,0], rand_mirror = False,
                 data_name='data', label_name='softmax_label', **kwargs):
        super(FaceImageIter2, self).__init__()
        if path_imgrec:
            logging.info('loading recordio %s...',
                         path_imgrec)
            path_imgidx = path_imgrec[0:-4]+".idx"
            self.imgrec = recordio.MXIndexedRecordIO(path_imgidx, path_imgrec, 'r')  # pylint: disable=redefined-variable-type
            self.imgidx = list(self.imgrec.keys)
            if shuffle:
              self.seq = self.imgidx
            else:
              self.seq = None
        else:
            self.imgrec = None
            assert path_imglist
            print('loading image list...')
            with open(path_imglist) as fin:
                imglist = {}
                imgkeys = []
                key = 0
                for line in iter(fin.readline, ''):
                    line = line.strip().split('\t')
                    flag = int(line[0])
                    if flag==0:
                      assert len(line)==17
                    else:
                      assert len(line)==3
                    label = nd.array([float(line[2])])
                    ilabel = int(line[2])
                    bbox = None
                    landmark = None
                    if len(line)==17:
                      bbox = np.array([int(i) for i in line[3:7]])
                      landmark = np.array([float(i) for i in line[7:17]]).reshape( (2,5) ).T
                    image_path = line[1]
                    if exclude_lfw:
                      _vec = image_path.split('/')
                      person_id = int(_vec[-2])
                      if person_id==166921 or person_id==1056413 or person_id==1193098:
                        continue
                    imglist[key] = (label, image_path, bbox, landmark)
                    imgkeys.append(key)
                    key+=1
                    #if key>=10000:
                    #  break
                self.imglist = imglist
            print('image list size', len(self.imglist))
            self.seq = imgkeys

        self.path_root = path_root
        self.mean = mean
        self.nd_mean = None
        if self.mean:
          self.mean = np.array(self.mean, dtype=np.float32).reshape(1,1,3)
          self.nd_mean = mx.nd.array(self.mean).reshape((1,1,3))
        self.patch = patch

        self.check_data_shape(data_shape)
        self.provide_data = [(data_name, (batch_size,) + data_shape)]
        self.provide_label = [(label_name, (batch_size,))]
        self.batch_size = batch_size
        self.data_shape = data_shape
        self.shuffle = shuffle
        self.image_size = '%d,%d'%(data_shape[1],data_shape[2])
        self.rand_mirror = rand_mirror
        #self.cast_aug = mx.image.CastAug()
        #self.color_aug = mx.image.ColorJitterAug(0.4, 0.4, 0.4)

        if aug_list is None:
            self.auglist = mx.image.CreateAugmenter(data_shape, **kwargs)
        else:
            self.auglist = aug_list
        print('aug size:', len(self.auglist))
        for aug in self.auglist:
          print(aug.__class__)
        self.cur = 0
        self.reset()

    def reset(self):
        """Resets the iterator to the beginning of the data."""
        print('call reset()')
        if self.shuffle:
            random.shuffle(self.seq)
        if self.imgrec is not None:
            self.imgrec.reset()
        self.cur = 0

    def num_samples(self):
      return len(self.seq)

    def next_sample(self):
        """Helper function for reading in next sample."""
        #set total batch size, for example, 1800, and maximum size for each people, for example 45
        if self.seq is not None:
          if self.cur >= len(self.seq):
              raise StopIteration
          idx = self.seq[self.cur]
          self.cur += 1
          if self.imgrec is not None:
            s = self.imgrec.read_idx(idx)
            header, img = recordio.unpack(s)
            return header.label, img, None, None
          else:
            label, fname, bbox, landmark = self.imglist[idx]
            return label, self.read_image(fname), bbox, landmark
        else:
            s = self.imgrec.read()
            if s is None:
                raise StopIteration
            header, img = recordio.unpack(s)
            return header.label, img, None, None

    def brightness_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      src *= alpha
      return src

    def contrast_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      coef = np.array([[[0.299, 0.587, 0.114]]])
      gray = src * coef
      gray = (3.0 * (1.0 - alpha) / gray.size) * np.sum(gray)
      src *= alpha
      src += gray
      return src

    def saturation_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      coef = np.array([[[0.299, 0.587, 0.114]]])
      gray = src * coef
      gray = np.sum(gray, axis=2, keepdims=True)
      gray *= (1.0 - alpha)
      src *= alpha
      src += gray
      return src

    def color_aug(self, img, x):
      augs = [self.brightness_aug, self.contrast_aug, self.saturation_aug]
      random.shuffle(augs)
      for aug in augs:
        #print(img.shape)
        img = aug(img, x)
        #print(img.shape)
      return img

    def mirror_aug(self, img):
      _rd = random.randint(0,1)
      if _rd==1:
        for c in xrange(img.shape[2]):
          img[:,:,c] = np.fliplr(img[:,:,c])
      return img


    def next(self):
        """Returns the next batch of data."""
        #print('in next', self.cur, self.labelcur)
        batch_size = self.batch_size
        c, h, w = self.data_shape
        batch_data = nd.empty((batch_size, c, h, w))
        batch_label = nd.empty(self.provide_label[0][1])
        i = 0
        try:
            while i < batch_size:
                label, s, bbox, landmark = self.next_sample()
                _data = self.imdecode(s)
                if self.rand_mirror:
                  _rd = random.randint(0,1)
                  if _rd==1:
                    _data = mx.ndarray.flip(data=_data, axis=1)
                if self.nd_mean is not None:
                    _data = _data.astype('float32')
                    _data -= self.nd_mean
                    _data *= 0.0078125
                #_npdata = _data.asnumpy()
                #if landmark is not None:
                #  _npdata = face_preprocess.preprocess(_npdata, bbox = bbox, landmark=landmark, image_size=self.image_size)
                #if self.rand_mirror:
                #  _npdata = self.mirror_aug(_npdata)
                #if self.mean is not None:
                #  _npdata = _npdata.astype(np.float32)
                #  _npdata -= self.mean
                #  _npdata *= 0.0078125
                #nimg = np.zeros(_npdata.shape, dtype=np.float32)
                #nimg[self.patch[1]:self.patch[3],self.patch[0]:self.patch[2],:] = _npdata[self.patch[1]:self.patch[3], self.patch[0]:self.patch[2], :]
                #_data = mx.nd.array(nimg)
                data = [_data]
                try:
                    self.check_valid_image(data)
                except RuntimeError as e:
                    logging.debug('Invalid image, skipping:  %s', str(e))
                    continue
                #print('aa',data[0].shape)
                #data = self.augmentation_transform(data)
                #print('bb',data[0].shape)
                for datum in data:
                    assert i < batch_size, 'Batch size must be multiples of augmenter output length'
                    #print(datum.shape)
                    batch_data[i][:] = self.postprocess_data(datum)
                    batch_label[i][:] = label
                    i += 1
        except StopIteration:
            if i<batch_size:
                raise StopIteration

        #print('next end', batch_size, i)
        return io.DataBatch([batch_data], [batch_label], batch_size - i)

    def check_data_shape(self, data_shape):
        """Checks if the input data shape is valid"""
        if not len(data_shape) == 3:
            raise ValueError('data_shape should have length 3, with dimensions CxHxW')
        if not data_shape[0] == 3:
            raise ValueError('This iterator expects inputs to have 3 channels.')

    def check_valid_image(self, data):
        """Checks if the input data is valid"""
        if len(data[0].shape) == 0:
            raise RuntimeError('Data shape is wrong')

    def imdecode(self, s):
        """Decodes a string or byte string to an NDArray.
        See mx.img.imdecode for more details."""
        #arr = np.fromstring(s, np.uint8)
        if self.patch[4]%2==0:
          img = mx.image.imdecode(s) #mx.ndarray
          #img = cv2.imdecode(arr, cv2.CV_LOAD_IMAGE_COLOR)
          #img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        else:
          img = mx.image.imdecode(s, flag=0)
          img = nd.broadcast_to(img, (img.shape[0], img.shape[1], 3))
          #img = cv2.imdecode(arr, cv2.CV_LOAD_IMAGE_GRAY)
        #img = np.float32(img)
        return img

    def read_image(self, fname):
        """Reads an input image `fname` and returns the decoded raw bytes.

        Example usage:
        ----------
        >>> dataIter.read_image('Face.jpg') # returns decoded raw bytes.
        """
        with open(os.path.join(self.path_root, fname), 'rb') as fin:
            img = fin.read()
        return img

    def augmentation_transform(self, data):
        """Transforms input data with specified augmentation."""
        for aug in self.auglist:
            data = [ret for src in data for ret in aug(src)]
        return data

    def postprocess_data(self, datum):
        """Final postprocessing step before image is loaded into the batch."""
        return nd.transpose(datum, axes=(2, 0, 1))

class FaceImageIter3(io.DataIter):

    def __init__(self, batch_size, ctx_num, images_per_identity, data_shape,
                 path_imgrec = None,
                 shuffle=False, mean = None, use_extra = False, model = None,
                 patch = [0,0,96,112,0], rand_mirror = False,
                 data_name='data', label_name='softmax_label', **kwargs):
        super(FaceImageIter3, self).__init__()
        assert(path_imgrec)
        logging.info('loading recordio %s...',
                     path_imgrec)
        path_imgidx = path_imgrec[0:-4]+".idx"
        self.imgrec = recordio.MXIndexedRecordIO(path_imgidx, path_imgrec, 'r')  # pylint: disable=redefined-variable-type
        #self.imgidx = list(self.imgrec.keys)
        s = self.imgrec.read_idx(0)
        header, _ = recordio.unpack(s)
        assert(header.flag==1)
        self.seq = range(1, int(header.label[0]))
        self.idx2range = {}
        self.seq_identity = range(int(header.label[0]), int(header.label[1]))
        for identity in self.seq_identity:
          s = self.imgrec.read_idx(identity)
          header, _ = recordio.unpack(s)
          assert(header.flag==2)
          self.idx2range[identity] = (int(header.label[0]), int(header.label[1]))
        print('idx2range', len(self.idx2range))


        self.path_root = path_root
        self.mean = mean
        self.nd_mean = None
        if self.mean:
          self.mean = np.array(self.mean, dtype=np.float32).reshape(1,1,3)
          self.nd_mean = mx.nd.array(self.mean).reshape((1,1,3))
        self.patch = patch

        self.check_data_shape(data_shape)
        self.provide_data = [(data_name, (batch_size,) + data_shape)]
        self.provide_label = [(label_name, (batch_size,))]
        self.batch_size = batch_size
        self.data_shape = data_shape
        self.shuffle = shuffle
        self.image_size = '%d,%d'%(data_shape[1],data_shape[2])
        self.rand_mirror = rand_mirror
        self.ctx_num = ctx_num 
        self.images_per_identity = images_per_identity
        self.identities = int(per_batch_size/self.images_per_identity)
        self.min_per_identity = 1
        assert self.min_per_identity<=self.images_per_identity
        print(self.images_per_identity, self.identities, self.min_per_identity)
        self.extra = None
        self.model = model
        if use_extra:
          self.provide_data = [(data_name, (batch_size,) + data_shape), ('extra', (batch_size, per_batch_size))]
          self.extra = np.full(self.provide_data[1][1], -1.0, dtype=np.float32)
          c = 0
          while c<batch_size:
            a = 0
            while a<per_batch_size:
              b = a+images_per_identity
              self.extra[(c+a):(c+b),a:b] = 1.0
              #print(c+a, c+b, a, b)
              a = b
            c += per_batch_size
          self.extra = nd.array(self.extra)
          print(self.extra)
        else:
          self.provide_data = [(data_name, (batch_size,) + data_shape)]
        self.cur = [0,0]
        self.reset()
        self.inited = False

    def offline_reset(self):
      self.seq_sim_identity = []
      data = nd.zeros( self.provide_data[0][1] )
      label = nd.zeros( self.provide_label[0][1] )
      #label = np.zeros( self.provide_label[0][1] )
      X = None
      ba = 0
      batch_num = 0
      while ba<len(self.seq):
        batch_num+=1
        if batch_num%10==0:
          print('loading batch',batch_num, ba)
        bb = min(ba+self.batch_size, len(self.seq))
        _count = bb-ba
        for i in xrange(_count):
          key = self.seq[i+ba]
          _label, fname, bbox, landmark = self.imglist[key]
          s = self.read_image(fname)
          _data = self.imdecode(s)
          #_data = self.augmentation_transform([_data])[0]
          _npdata = _data.asnumpy()
          if landmark is not None:
            _npdata = face_preprocess.preprocess(_npdata, bbox = bbox, landmark=landmark, image_size=self.image_size)
          if self.mean is not None:
            _npdata = _npdata.astype(np.float32)
            _npdata -= self.mean
            _npdata *= 0.0078125
          nimg = np.zeros(_npdata.shape, dtype=np.float32)
          nimg[self.patch[1]:self.patch[3],self.patch[0]:self.patch[2],:] = _npdata[self.patch[1]:self.patch[3], self.patch[0]:self.patch[2], :]
          #print(_npdata.shape)
          #print(_npdata)
          _data = mx.nd.array(nimg)
          data[i][:] = self.postprocess_data(_data)
          label[i][:] = _label
        db = mx.io.DataBatch(data=(data,self.extra), label=(label,))
        self.model.forward(db, is_train=False)
        net_out = self.model.get_outputs()
        _embeddings = net_out[0].asnumpy()
        _embeddings = sklearn.preprocessing.normalize(_embeddings)
        if _count<self.batch_size:
          _embeddings = _embeddings[0:_count,:]
        #print(_embeddings.shape)
        if X is None:
          X = np.zeros( (len(self.olabels), _embeddings.shape[1]), dtype=np.float32 )
        nplabel = label.asnumpy()
        for i in xrange(_count):
          ilabel = int(nplabel[i])
          #print(ilabel, ilabel.__class__)
          X[ilabel] += _embeddings[i]
        ba = bb
      X = sklearn.preprocessing.normalize(X)
      d = X.shape[1]
      faiss_params = [20,5]
      print('start to train faiss')
      print(X.shape)
      quantizer = faiss.IndexFlatL2(d)  # the other index
      index = faiss.IndexIVFFlat(quantizer, d, faiss_params[0], faiss.METRIC_L2)
      assert not index.is_trained
      index.train(X)
      index.add(X)
      assert index.is_trained
      print('trained')
      index.nprobe = faiss_params[1]
      k = self.identities
      D, I = index.search(X, k)     # actual search
      print(I.shape)
      self.labels = []
      for i in xrange(I.shape[0]):
        #assert I[i][0]==i
        for j in xrange(k):
          _label = I[i][j]
          assert _label<len(self.olabels)
          self.labels.append(_label)
      print('labels assigned', len(self.labels))

    def reset(self):
        """Resets the iterator to the beginning of the data."""
        print('call reset()')
        if self.shuffle:
            offline_reset()
            random.shuffle(self.seq)
            random.shuffle(self.seq_identity)
        if self.imgrec is not None:
            self.imgrec.reset()
        self.cur = [0,0]

    def num_samples(self):
      return len(self.seq)

    def next_sample(self):
        """Helper function for reading in next sample."""
        #set total batch size, for example, 1800, and maximum size for each people, for example 45
        while True:
          if self.cur[0] >= len(self.seq_sim_identity):
              raise StopIteration
          identity = self.seq_sim_identity[self.cur[0]]
          if self.cur[1]>=self.images_per_identity:
            self.cur[0]+=1
            self.cur[1]=0
            s = self.imgrec.read_idx(identity)
            header, _ = recordio.unpack(s)
            self.idx_range = range(int(header.label[0]), int(header.label[1]))
            continue
          if self.shuffle and self.cur[1]==0:
            random.shuffle(self.idx_range)
          idx = self.idx_range[self.cur[1]]
          self.cur[1] += 1
          s = self.imgrec.read_idx(idx)
          header, img = recordio.unpack(s)
          return header.label, img, None, None


    def brightness_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      src *= alpha
      return src

    def contrast_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      coef = np.array([[[0.299, 0.587, 0.114]]])
      gray = src * coef
      gray = (3.0 * (1.0 - alpha) / gray.size) * np.sum(gray)
      src *= alpha
      src += gray
      return src

    def saturation_aug(self, src, x):
      alpha = 1.0 + random.uniform(-x, x)
      coef = np.array([[[0.299, 0.587, 0.114]]])
      gray = src * coef
      gray = np.sum(gray, axis=2, keepdims=True)
      gray *= (1.0 - alpha)
      src *= alpha
      src += gray
      return src

    def color_aug(self, img, x):
      augs = [self.brightness_aug, self.contrast_aug, self.saturation_aug]
      random.shuffle(augs)
      for aug in augs:
        #print(img.shape)
        img = aug(img, x)
        #print(img.shape)
      return img

    def mirror_aug(self, img):
      _rd = random.randint(0,1)
      if _rd==1:
        for c in xrange(img.shape[2]):
          img[:,:,c] = np.fliplr(img[:,:,c])
      return img


    def next(self):
        if not self.inited:
          self.reset()
          self.inited = True
        """Returns the next batch of data."""
        #print('in next', self.cur, self.labelcur)
        batch_size = self.batch_size
        c, h, w = self.data_shape
        batch_data = nd.empty((batch_size, c, h, w))
        batch_label = nd.empty(self.provide_label[0][1])
        i = 0
        try:
            while i < batch_size:
                label, s, bbox, landmark = self.next_sample()
                _data = self.imdecode(s)
                if self.rand_mirror:
                  _rd = random.randint(0,1)
                  if _rd==1:
                    _data = mx.ndarray.flip(data=_data, axis=1)
                if self.nd_mean is not None:
                    _data = _data.astype('float32')
                    _data -= self.nd_mean
                    _data *= 0.0078125
                data = [_data]
                try:
                    self.check_valid_image(data)
                except RuntimeError as e:
                    logging.debug('Invalid image, skipping:  %s', str(e))
                    continue
                #print('aa',data[0].shape)
                #data = self.augmentation_transform(data)
                #print('bb',data[0].shape)
                for datum in data:
                    assert i < batch_size, 'Batch size must be multiples of augmenter output length'
                    #print(datum.shape)
                    batch_data[i][:] = self.postprocess_data(datum)
                    batch_label[i][:] = label
                    i += 1
        except StopIteration:
            if i<batch_size:
                raise StopIteration

        #print('next end', batch_size, i)
        return io.DataBatch([batch_data], [batch_label], batch_size - i)

    def check_data_shape(self, data_shape):
        """Checks if the input data shape is valid"""
        if not len(data_shape) == 3:
            raise ValueError('data_shape should have length 3, with dimensions CxHxW')
        if not data_shape[0] == 3:
            raise ValueError('This iterator expects inputs to have 3 channels.')

    def check_valid_image(self, data):
        """Checks if the input data is valid"""
        if len(data[0].shape) == 0:
            raise RuntimeError('Data shape is wrong')

    def imdecode(self, s):
        """Decodes a string or byte string to an NDArray.
        See mx.img.imdecode for more details."""
        img = mx.image.imdecode(s) #mx.ndarray
        return img

    def read_image(self, fname):
        """Reads an input image `fname` and returns the decoded raw bytes.

        Example usage:
        ----------
        >>> dataIter.read_image('Face.jpg') # returns decoded raw bytes.
        """
        with open(os.path.join(self.path_root, fname), 'rb') as fin:
            img = fin.read()
        return img

    def augmentation_transform(self, data):
        """Transforms input data with specified augmentation."""
        for aug in self.auglist:
            data = [ret for src in data for ret in aug(src)]
        return data

    def postprocess_data(self, datum):
        """Final postprocessing step before image is loaded into the batch."""
        return nd.transpose(datum, axes=(2, 0, 1))

class FaceImageIter4(io.DataIter):

    def __init__(self, batch_size, ctx_num, images_per_identity, data_shape, 
        path_imglist=None, path_root=None,
        shuffle=False, aug_list=None, exclude_lfw = False, mean = None, use_extra = False, model = None,
        patch = [0,0,96,112,0],  rand_mirror = False,
        data_name='data', label_name='softmax_label', **kwargs):
        super(FaceImageIter4, self).__init__()
        assert path_imglist
        print('loading image list...')
        with open(path_imglist) as fin:
            self.imglist = {}
            self.imgkeys = []
            self.labels = []
            self.olabels = []
            self.labelposting = {}
            self.seq = []
            key = 0
            for line in iter(fin.readline, ''):
                line = line.strip().split('\t')
                flag = int(line[0])
                if flag==0:
                  assert len(line)==17
                else:
                  assert len(line)==3
                label = nd.array([float(line[2])])
                ilabel = int(line[2])
                bbox = None
                landmark = None
                if len(line)==17:
                  bbox = np.array([int(i) for i in line[3:7]])
                  landmark = np.array([float(i) for i in line[7:17]]).reshape( (2,5) ).T
                image_path = line[1]
                if exclude_lfw:
                  _vec = image_path.split('/')
                  person_id = int(_vec[-2])
                  if person_id==166921 or person_id==1056413 or person_id==1193098:
                    continue
                self.imglist[key] = (label, image_path, bbox, landmark)
                self.seq.append(key)
                if ilabel in self.labelposting:
                  self.labelposting[ilabel].append(key)
                else:
                  self.labelposting[ilabel] = [key]
                  self.olabels.append(ilabel)
                key+=1
                #if key>=10000:
                #  break
        print('image list size', len(self.imglist))
        print('label size', len(self.olabels))
        print('last label',self.olabels[-1])

        self.path_root = path_root
        self.mean = mean
        if self.mean:
          self.mean = np.array(self.mean, dtype=np.float32).reshape(1,1,3)
        self.patch = patch 

        self.check_data_shape(data_shape)
        per_batch_size = int(batch_size/ctx_num)
        self.provide_label = [(label_name, (batch_size,))]
        self.batch_size = batch_size
        self.data_shape = data_shape
        self.shuffle = shuffle
        self.image_size = '%d,%d'%(data_shape[1],data_shape[2])
        self.rand_mirror = rand_mirror
        print('rand_mirror', self.rand_mirror)
        self.extra = None
        self.model = model
        if use_extra:
          self.provide_data = [(data_name, (batch_size,) + data_shape), ('extra', (batch_size, per_batch_size))]
          self.extra = np.full(self.provide_data[1][1], -1.0, dtype=np.float32)
          c = 0
          while c<batch_size:
            a = 0
            while a<per_batch_size:
              b = a+images_per_identity
              self.extra[(c+a):(c+b),a:b] = 1.0
              #print(c+a, c+b, a, b)
              a = b
            c += per_batch_size
          self.extra = nd.array(self.extra)
          #self.batch_label = nd.empty(self.provide_label[0][1])
          #per_batch_size = int(batch_size/ctx_num)
          #_label = -1
          #for i in xrange(batch_size):
          #  if i%self.images_per_identity==0:
          #    _label+=1
          #    if i%per_batch_size==0:
          #      _label = 0
          #  label = nd.array([float(_label)])
          #  self.batch_label[i][:] = label
          #print(self.batch_label)
          print(self.extra)
        else:
          self.provide_data = [(data_name, (batch_size,) + data_shape)]
        self.ctx_num = ctx_num 
        self.images_per_identity = images_per_identity
        self.identities = int(per_batch_size/self.images_per_identity)
        self.min_per_identity = 10
        if self.images_per_identity<=10:
          self.min_per_identity = self.images_per_identity
        self.min_per_identity = 1
        assert self.min_per_identity<=self.images_per_identity
        print(self.images_per_identity, self.identities, self.min_per_identity)

        if aug_list is None:
            self.auglist = mx.image.CreateAugmenter(data_shape, **kwargs)
        else:
            self.auglist = aug_list
        print('aug size:', len(self.auglist))
        for aug in self.auglist:
          print(aug.__class__)
        self.cur = [0, 0]
        self.inited = False

    def get_extra(self):
      return self.extra

    def offline_reset(self):
      data = nd.zeros( self.provide_data[0][1] )
      label = nd.zeros( self.provide_label[0][1] )
      #label = np.zeros( self.provide_label[0][1] )
      X = None
      ba = 0
      batch_num = 0
      while ba<len(self.seq):
        batch_num+=1
        if batch_num%10==0:
          print('loading batch',batch_num, ba)
        bb = min(ba+self.batch_size, len(self.seq))
        _count = bb-ba
        for i in xrange(_count):
          key = self.seq[i+ba]
          _label, fname, bbox, landmark = self.imglist[key]
          s = self.read_image(fname)
          _data = self.imdecode(s)
          #_data = self.augmentation_transform([_data])[0]
          _npdata = _data.asnumpy()
          if landmark is not None:
            _npdata = face_preprocess.preprocess(_npdata, bbox = bbox, landmark=landmark, image_size=self.image_size)
          if self.mean is not None:
            _npdata = _npdata.astype(np.float32)
            _npdata -= self.mean
            _npdata *= 0.0078125
          nimg = np.zeros(_npdata.shape, dtype=np.float32)
          nimg[self.patch[1]:self.patch[3],self.patch[0]:self.patch[2],:] = _npdata[self.patch[1]:self.patch[3], self.patch[0]:self.patch[2], :]
          #print(_npdata.shape)
          #print(_npdata)
          _data = mx.nd.array(nimg)
          data[i][:] = self.postprocess_data(_data)
          label[i][:] = _label
        db = mx.io.DataBatch(data=(data,self.extra), label=(label,))
        self.model.forward(db, is_train=False)
        net_out = self.model.get_outputs()
        _embeddings = net_out[0].asnumpy()
        _embeddings = sklearn.preprocessing.normalize(_embeddings)
        if _count<self.batch_size:
          _embeddings = _embeddings[0:_count,:]
        #print(_embeddings.shape)
        if X is None:
          X = np.zeros( (len(self.olabels), _embeddings.shape[1]), dtype=np.float32 )
        nplabel = label.asnumpy()
        for i in xrange(_count):
          ilabel = int(nplabel[i])
          #print(ilabel, ilabel.__class__)
          X[ilabel] += _embeddings[i]
        ba = bb
      X = sklearn.preprocessing.normalize(X)
      d = X.shape[1]
      faiss_params = [20,5]
      print('start to train faiss')
      print(X.shape)
      quantizer = faiss.IndexFlatL2(d)  # the other index
      index = faiss.IndexIVFFlat(quantizer, d, faiss_params[0], faiss.METRIC_L2)
      assert not index.is_trained
      index.train(X)
      index.add(X)
      assert index.is_trained
      print('trained')
      index.nprobe = faiss_params[1]
      k = self.identities
      D, I = index.search(X, k)     # actual search
      print(I.shape)
      self.labels = []
      for i in xrange(I.shape[0]):
        #assert I[i][0]==i
        for j in xrange(k):
          _label = I[i][j]
          assert _label<len(self.olabels)
          self.labels.append(_label)
      print('labels assigned', len(self.labels))


      

    def reset(self):
        """Resets the iterator to the beginning of the data."""
        print('call reset()')
        if self.extra is not None:
          self.offline_reset()
        elif self.shuffle:
          random.shuffle(self.labels)
        self.cur = [0,0]

    def num_samples(self):
      #count = 0
      #for k,v in self.labelposting.iteritems():
      #  if len(v)<self.min_per_identity:
      #    continue
      #  count+=self.images_per_identity
      count = len(self.olabels)*self.images_per_identity*self.identities
      return count


    def next_sample(self):
        """Helper function for reading in next sample."""
        #set total batch size, for example, 1800, and maximum size for each people, for example 45
        while True:
          if self.cur[0] >= len(self.labels):
            raise StopIteration
          label = self.labels[self.cur[0]]
          posting = self.labelposting[label]
          if len(posting)<self.min_per_identity or self.cur[1] >= self.images_per_identity:
            self.cur[0]+=1
            self.cur[1] = 0
            continue
          if self.shuffle and self.cur[1]==0:
            random.shuffle(posting)
          idx = posting[self.cur[1]%len(posting)]
          self.cur[1] += 1
          label, fname, bbox, landmark = self.imglist[idx]
          return label, self.read_image(fname), bbox, landmark


    def next(self):
        if not self.inited:
          self.reset()
          self.inited = True
        """Returns the next batch of data."""
        #print('in next', self.cur, self.labelcur)
        batch_size = self.batch_size
        c, h, w = self.data_shape
        batch_data = nd.empty((batch_size, c, h, w))
        batch_label = nd.empty(self.provide_label[0][1])
        i = 0
        try:
            while i < batch_size:
                label, s, bbox, landmark = self.next_sample()
                _data = self.imdecode(s)
                if self.rand_mirror:
                  _rd = random.randint(0,1)
                  if _rd==1:
                    _data = mx.ndarray.flip(data=_data, axis=1)
                if self.nd_mean is not None:
                    _data = _data.astype('float32')
                    _data -= self.nd_mean
                    _data *= 0.0078125
                data = [_data]
                try:
                    self.check_valid_image(data)
                except RuntimeError as e:
                    logging.debug('Invalid image, skipping:  %s', str(e))
                    continue
                #print('aa',data[0].shape)
                #data = self.augmentation_transform(data)
                #print('bb',data[0].shape)
                for datum in data:
                    assert i < batch_size, 'Batch size must be multiples of augmenter output length'
                    #print(datum.shape)
                    batch_data[i][:] = self.postprocess_data(datum)
                    batch_label[i][:] = label
                    i += 1
        except StopIteration:
            if i<batch_size:
                raise StopIteration

        #print('next end', batch_size, i)
        if self.extra is not None:
          return io.DataBatch([batch_data, self.extra], [batch_label], batch_size - i)
        else:
          return io.DataBatch([batch_data], [batch_label], batch_size - i)

    def check_data_shape(self, data_shape):
        """Checks if the input data shape is valid"""
        if not len(data_shape) == 3:
            raise ValueError('data_shape should have length 3, with dimensions CxHxW')
        if not data_shape[0] == 3:
            raise ValueError('This iterator expects inputs to have 3 channels.')

    def check_valid_image(self, data):
        """Checks if the input data is valid"""
        if len(data[0].shape) == 0:
            raise RuntimeError('Data shape is wrong')

    def imdecode(self, s):
        """Decodes a string or byte string to an NDArray.
        See mx.img.imdecode for more details."""
        if self.patch[4]%2==0:
          img = mx.image.imdecode(s)
        else:
          img = mx.image.imdecode(s, flag=0)
          img = nd.broadcast_to(img, (img.shape[0], img.shape[1], 3))
        return img

    def read_image(self, fname):
        """Reads an input image `fname` and returns the decoded raw bytes.

        Example usage:
        ----------
        >>> dataIter.read_image('Face.jpg') # returns decoded raw bytes.
        """
        with open(os.path.join(self.path_root, fname), 'rb') as fin:
            img = fin.read()
        return img

    def augmentation_transform(self, data):
        """Transforms input data with specified augmentation."""
        for aug in self.auglist:
            data = [ret for src in data for ret in aug(src)]
        return data

    def postprocess_data(self, datum):
        """Final postprocessing step before image is loaded into the batch."""
        return nd.transpose(datum, axes=(2, 0, 1))

class FaceImageIter5(io.DataIter):

    def __init__(self, batch_size, ctx_num, images_per_identity, data_shape, 
        path_imglist=None, path_root=None,
        shuffle=False, aug_list=None, exclude_lfw = False, mean = None,
        patch = [0,0,96,112,0],  rand_mirror = False,
        data_name='data', label_name='softmax_label', **kwargs):
        super(FaceImageIter5, self).__init__()
        assert path_imglist
        print('loading image list...')
        with open(path_imglist) as fin:
            self.imglist = {}
            self.labels = []
            self.olabels = []
            self.labelposting = {}
            self.seq = []
            key = 0
            for line in iter(fin.readline, ''):
                line = line.strip().split('\t')
                flag = int(line[0])
                if flag==0:
                  assert len(line)==17
                else:
                  assert len(line)==3
                label = nd.array([float(line[2])])
                ilabel = int(line[2])
                bbox = None
                landmark = None
                if len(line)==17:
                  bbox = np.array([int(i) for i in line[3:7]])
                  landmark = np.array([float(i) for i in line[7:17]]).reshape( (2,5) ).T
                image_path = line[1]
                if exclude_lfw:
                  _vec = image_path.split('/')
                  person_id = int(_vec[-2])
                  if person_id==166921 or person_id==1056413 or person_id==1193098:
                    continue
                self.imglist[key] = (label, image_path, bbox, landmark)
                self.seq.append(key)
                if ilabel in self.labelposting:
                  self.labelposting[ilabel].append(key)
                else:
                  self.labelposting[ilabel] = [key]
                  self.olabels.append(ilabel)
                key+=1
                #if key>=10000:
                #  break
        print('image list size', len(self.imglist))
        print('label size', len(self.olabels))
        print('last label',self.olabels[-1])

        self.path_root = path_root
        self.mean = mean
        if self.mean:
          self.mean = np.array(self.mean, dtype=np.float32).reshape(1,1,3)
        self.patch = patch 

        self.check_data_shape(data_shape)
        self.per_batch_size = int(batch_size/ctx_num)
        self.provide_label = [(label_name, (batch_size,))]
        self.batch_size = batch_size
        self.ctx_num = ctx_num 
        self.images_per_identity = images_per_identity
        self.identities = int(self.per_batch_size/self.images_per_identity)
        self.min_per_identity = 10
        if self.images_per_identity<=10:
          self.min_per_identity = self.images_per_identity
        self.min_per_identity = 1
        assert self.min_per_identity<=self.images_per_identity
        print(self.images_per_identity, self.identities, self.min_per_identity)
        self.data_shape = data_shape
        self.shuffle = shuffle
        self.image_size = '%d,%d'%(data_shape[1],data_shape[2])
        self.rand_mirror = rand_mirror
        print('rand_mirror', self.rand_mirror)
        self.provide_data = [(data_name, (batch_size,) + data_shape)]

        if aug_list is None:
            self.auglist = mx.image.CreateAugmenter(data_shape, **kwargs)
        else:
            self.auglist = aug_list
        print('aug size:', len(self.auglist))
        for aug in self.auglist:
          print(aug.__class__)
        self.cur = 0
        self.buffer = []
        self.reset()


    def reset(self):
        """Resets the iterator to the beginning of the data."""
        print('call reset()')
        if self.shuffle:
          random.shuffle(self.seq)
        self.cur = 0

    def num_samples(self):
      return -1


    def next_sample(self, i_ctx):
        if self.cur >= len(self.seq):
          raise StopIteration
        if i_ctx==0:
          idx = self.seq[self.cur]
          self.cur += 1
          label, fname, bbox, landmark = self.imglist[idx]
          ilabel = int(label.asnumpy()[0])
          self.buffer = self.labelposting[ilabel]
          random.shuffle(self.buffer)
        if i_ctx<self.images_per_identity:
          pos = i_ctx%len(self.buffer)
          idx = self.buffer[pos]
        else:
          idx = self.seq[self.cur]
          self.cur += 1
        label, fname, bbox, landmark = self.imglist[idx]
        return label, self.read_image(fname), bbox, landmark


    def next(self):
        """Returns the next batch of data."""
        #print('in next', self.cur, self.labelcur)
        batch_size = self.batch_size
        c, h, w = self.data_shape
        batch_data = nd.empty((batch_size, c, h, w))
        batch_label = nd.empty(self.provide_label[0][1])
        i = 0
        try:
            while i < batch_size:
                i_ctx = i%self.per_batch_size
                label, s, bbox, landmark = self.next_sample(i_ctx)
                _data = self.imdecode(s)
                #_data = self.augmentation_transform([_data])[0]
                _npdata = _data.asnumpy()
                if landmark is not None:
                  _npdata = face_preprocess.preprocess(_npdata, bbox = bbox, landmark=landmark, image_size=self.image_size)
                if self.rand_mirror:
                  _rd = random.randint(0,1)
                  if _rd==1:
                    for c in xrange(_npdata.shape[2]):
                      _npdata[:,:,c] = np.fliplr(_npdata[:,:,c])
                if self.mean is not None:
                  _npdata = _npdata.astype(np.float32)
                  _npdata -= self.mean
                  _npdata *= 0.0078125
                nimg = np.zeros(_npdata.shape, dtype=np.float32)
                nimg[self.patch[1]:self.patch[3],self.patch[0]:self.patch[2],:] = _npdata[self.patch[1]:self.patch[3], self.patch[0]:self.patch[2], :]
                #print(_npdata.shape)
                #print(_npdata)
                _data = mx.nd.array(nimg)
                #print(_data.shape)
                data = [_data]
                try:
                    self.check_valid_image(data)
                except RuntimeError as e:
                    logging.debug('Invalid image, skipping:  %s', str(e))
                    continue
                #print('aa',data[0].shape)
                #data = self.augmentation_transform(data)
                #print('bb',data[0].shape)
                for datum in data:
                    assert i < batch_size, 'Batch size must be multiples of augmenter output length'
                    #print(datum.shape)
                    batch_data[i][:] = self.postprocess_data(datum)
                    batch_label[i][:] = label
                    i += 1
        except StopIteration:
            if i<batch_size:
                raise StopIteration

        return io.DataBatch([batch_data], [batch_label], batch_size - i)

    def check_data_shape(self, data_shape):
        """Checks if the input data shape is valid"""
        if not len(data_shape) == 3:
            raise ValueError('data_shape should have length 3, with dimensions CxHxW')
        if not data_shape[0] == 3:
            raise ValueError('This iterator expects inputs to have 3 channels.')

    def check_valid_image(self, data):
        """Checks if the input data is valid"""
        if len(data[0].shape) == 0:
            raise RuntimeError('Data shape is wrong')

    def imdecode(self, s):
        """Decodes a string or byte string to an NDArray.
        See mx.img.imdecode for more details."""
        if self.patch[4]%2==0:
          img = mx.image.imdecode(s)
        else:
          img = mx.image.imdecode(s, flag=0)
          img = nd.broadcast_to(img, (img.shape[0], img.shape[1], 3))
        return img

    def read_image(self, fname):
        """Reads an input image `fname` and returns the decoded raw bytes.

        Example usage:
        ----------
        >>> dataIter.read_image('Face.jpg') # returns decoded raw bytes.
        """
        with open(os.path.join(self.path_root, fname), 'rb') as fin:
            img = fin.read()
        return img

    def augmentation_transform(self, data):
        """Transforms input data with specified augmentation."""
        for aug in self.auglist:
            data = [ret for src in data for ret in aug(src)]
        return data

    def postprocess_data(self, datum):
        """Final postprocessing step before image is loaded into the batch."""
        return nd.transpose(datum, axes=(2, 0, 1))
