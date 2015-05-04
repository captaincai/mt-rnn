#!/usr/bin/env python

"""
Wrapper script to train an RNN-encoder-decoder model for phrase translation probabilities
"""

import os
import sys
import gzip
import time
import codecs
import random
import pickle
import operator
import argparse
import numpy as np
from collections import defaultdict
import rnn_encoder_decoder as rnned

# For pickle to work properly
sys.setrecursionlimit(50000)


def readWordVectors(vectorBin, vocab, dim):
  """
  Reads the words embeddings generated by an external entity (word2vec)
  In theory, these can be random vectors as well

  Parameters:
    vectorBin : The binarized file containing the word embeddings
    vocab : The vocabulary for the language
    dim : The dimensionality of the word embedding
          (for sanity check only, to make sure you get what you expect)

  Returns:
    unkCount : The number of types in the vocab that are OOV wrt to the embeddings
    embeddings : The words embeddings for the words in the vocabulary
  """
  # First read vectors into a temporary hash
  vectorHash = defaultdict()
  with open(vectorBin) as fin:
    header = fin.readline()
    vocab_size, vector_size = map(int, header.split())

    assert vector_size == dim

    binary_len = np.dtype(np.float32).itemsize * vector_size
    for line_number in xrange(vocab_size):
      # mixed text and binary: read text first, then binary
      word = ''
      while True:
        ch = fin.read(1)
        if ch == ' ':
          break
        word += ch

      vector = np.fromstring(fin.read(binary_len), np.float32)
      vectorHash[word.decode('utf8')] = vector
      fin.read(1)  # newline

  # Now create the embedding matrix
  embeddings = np.empty((len(vocab), dim), dtype=np.float32)
  # Embedding for the unknown symbol
  unk = np.ones((dim))
  # We don't want to count the explicit UNK as an unknown
  unkCount = -1
  for i in range(len(vocab)):
    if vocab[i] not in vectorHash:
      unkCount += 1
    embeddings[i] = vectorHash.get(vocab[i], unk)

  del vectorHash
  return unkCount, embeddings


def parseCorpus(iFile, pruneThreshold):
  """
  Reads a corpus and generates a pruned vocabulary

  Parameters:
    iFile : The input file for the corpus (open file handle)
    pruneThreshold : The threshold for histogram pruning of the vocabulary

  Returns:
    coverage : What percentage of the corpus is covered by the pruned vocabulary
              We want a small vocab and high coverage
    vocab : Vocabulary (word -> id)
    rVocab : Reverse vocabulary (id -> word)
  """
  freq = defaultdict()
  for line in iFile:
    words = line.strip().split()
    for word in words:
      freq[word] = freq.get(word, 0) + 1

  # Sort the frequencies
  wordCounts = reduce(lambda x, y: x + y, freq.values())
  freqSort = sorted(freq.items(), key=operator.itemgetter(1), reverse=True)
  # Prune the vocab
  freqSort = freqSort[:pruneThreshold]
  prunedWordCounts = reduce(lambda x, y: x + y, [x[1] for x in freqSort])
  vocab = defaultdict()
  rVocab = defaultdict()
  vocab["UNK"] = 0
  rVocab[0] = "UNK"
  vocabID = 0
  for item in freqSort:
    vocabID += 1
    vocab[item[0]] = vocabID
    rVocab[vocabID] = item[0]

  return float(prunedWordCounts)/wordCounts, vocab, rVocab


def minibatch(l, bs):
  """
  Yield batches for mini-batch SGD

  Parameters:
    l : The list of training examples
    bs : The batch size

  Returns:
    Iterator over batches
  """
  for i in xrange(0, len(l), bs):
    yield l[i:i+bs]


def getPhrasePairs(tTable, sVocab, tVocab, sEmbeddings, tEmbeddings):
  """
  Reads a phrase table and gets phrase pairs for training

  Parameters:
    tTable : The phrase table (open file handle)
    sVocab : The vocabulary of the source language
    tVocab : The vocabulary of the target language
    sEmbeddings : The word embeddings for the source language
    tEmbeddings : The word embeddings for the target language

  Returns:
    phrasePairs : Tuples containing (source phrase vector, target phrase vector, target phrase ids)
  """
  phrasePairs = []
  for line in tTable:
    line = line.strip().split("|||")
    sPhrase = np.asarray([sVocab.get(w, 0) for w in line[0].strip().split()]).astype('int32')
    tPhrase = np.asarray([tVocab.get(w, 0) for w in line[1].strip().split()]).astype('int32')
    # Don't include phrases that contain only OOVs
    if np.sum(sPhrase) == 0 or np.sum(tPhrase) == 0:
      continue
    phrasePairs.append((sEmbeddings[sPhrase], tEmbeddings[tPhrase], tPhrase))

  return phrasePairs


def shuffle(l, seed):
  """
  Shuffles training samples (in-place)

  Parameters:
    l : The training samples
    seed : A seed for the RNG
  """
  random.seed(seed)
  random.shuffle(l)


def getPartitions(phrasePairs, seed):
  """
  Gets training and validation partitions (80/20) from a set of training samples

  Parameters:
    phrasePairs : The training samples
    seed : A seed for the RNG
  """
  shuffle(phrasePairs, seed)
  # 80/20 partition for train/dev
  return phrasePairs[:int(0.8 * len(phrasePairs))], phrasePairs[int(0.8 * len(phrasePairs)):]


def saveModel(outDir, sVocab, tVocab, sEmbedding, tEmbedding, rnn):
  """
  Pickles a model

  Parameters:
    outDir : The output directory (created if it does not exist)
    sVocab : The source vocabulary
    tVocab : The target vocabulary
    sEmbedding : The source word embeddings
    tEmbedding : The target word embeddings
    rnn : An RNN encoder-decoder model
  """
  os.system("mkdir -p " + outDir)
  os.system("mv " + outDir + "/best.mdl " + outDir + "/secondBest.mdl 2>/dev/null")
  with open(outDir + "/best.mdl", "wb") as m:
    pickle.dump([sVocab, tVocab, sEmbedding, tEmbedding, rnn], m)


parser = argparse.ArgumentParser("Runs the RNN encoder-decoder training procedure for machine translation")
parser.add_argument("-p", "--phrase-table", dest="phraseTable",
    default="/export/a04/gkumar/experiments/MT-JHU/1/model/phrase-table.tiny.1.gz", help="The location of the phrase table")
    #default="/export/a04/gkumar/experiments/MT-JHU/1/model/phrase-table.1.gz", help="The location of the phrase table")
parser.add_argument("-f", "--source", dest="sFile",
    default="/export/a04/gkumar/corpora/fishcall/kaldi_fishcall_output/SAT/ldc/processed/fisher_train.tok.lc.clean.es",
    help="The training text for the foreign (target) language")
parser.add_argument("-e", "--target", dest="tFile",
    default="/export/a04/gkumar/corpora/fishcall/kaldi_fishcall_output/SAT/ldc/processed/fisher_train.tok.lc.clean.en",
    help="The training text for the english (source) language")
parser.add_argument("-s", "--source-emb", dest="sEmbeddings",
    default="/export/a04/gkumar/code/custom/brae/tools/word2vec/fisher_es.vectors.50.sg.bin", help="Source embeddings obtained from word2vec")
parser.add_argument("-t", "--target-emb", dest="tEmbeddings",
    default="/export/a04/gkumar/code/custom/brae/tools/word2vec/fisher_en.vectors.50.sg.bin", help="Target embeddings obtained from word2vec")
parser.add_argument("-o", "--outdir", dest="outDir", default="data/1.tiny", help="An output directory where the model is written")
opts = parser.parse_args()


# Hyperparameters
s = {
  'lr': 0.0827, # The learning rate
  'bs':1000, # size of the mini-batch
  'nhidden':500, # Size of the hidden layer
  'seed':324, # Seed for the random number generator
  'emb_dimension':50, # The dimension of the embedding
  'nepochs':25, # The number of epochs that training is to run for
  'prune_t':5000 # The frequency threshold for histogram pruning of the vocab
}

# First process the training dataset and get the source and target vocabulary
start = time.time()
sCoverage, s2idx, idx2s = parseCorpus(codecs.open(opts.sFile, encoding="utf8"), s['prune_t'])
tCoverage, t2idx, idx2t = parseCorpus(codecs.open(opts.tFile, encoding="utf8"), s['prune_t'])
print "***", sCoverage*100, "% of the source corpus covered by the pruned vocabulary"
print "***", tCoverage*100, "% of the target corpus covered by the pruned vocabulary "
print "--- Done creating vocabularies : ", time.time() - start, "s"

# Get embeddings for the source and the target phrase pairs
start = time.time()
sUnkCount, sEmbeddings = readWordVectors(opts.sEmbeddings, idx2s, s['emb_dimension'])
tUnkCount, tEmbeddings = readWordVectors(opts.tEmbeddings, idx2t, s['emb_dimension'])
print "***", sUnkCount, " source types were not seen in the embeddings"
print "***", tUnkCount, " target types were not seen in the embeddings"
print "--- Done reading embeddings for source and target : ", time.time() - start, "s"

# Now, read the phrase table and get the phrase pairs for training
start = time.time()
phraseTable = gzip.open(opts.phraseTable)
phrasePairs = getPhrasePairs(phraseTable, s2idx, t2idx, sEmbeddings, tEmbeddings)
print "--- Done reading phrase pairs from the phrase table : ", time.time() - start, "s"

# Create the training and the dev partitions
train, dev = getPartitions(phrasePairs, s['seed'])

tVocSize = len(t2idx)
nTrainExamples = len(train)

start = time.time()
rnn = rnned.RNNED(nh=s['nhidden'], nc=tVocSize, de=s['emb_dimension'])
print "--- Done compiling theano functions : ", time.time() - start, "s"

# Training
best_dev_nll = np.inf
s['clr'] = s['lr']
for e in xrange(s['nepochs']):
  # Shuffle the examples
  shuffle(train, s['seed'])
  s['ce'] = e
  tic = time.time()
  for i, batch in enumerate(minibatch(train, s['bs'])):
    rnn.train(batch, s['clr'])

  print '[learning] epoch', e,  '>> completed in', time.time() - tic, '(sec) <<'
  sys.stdout.flush()

  # Get the average NLL For the validation set
  dev_nll = rnn.test(dev)
  print '[dev-nll]', dev_nll, "(NEW BEST)" if dev_nll < best_dev_nll else ""

  if dev_nll < best_dev_nll:
    best_dev_nll = dev_nll
    s['be'] = e
    saveModel(opts.outDir, s2idx, t2idx, sEmbeddings, tEmbeddings, rnn)

  # Decay learning rate if there's no improvement in 3 epochs
  if abs(s['be'] - s['ce']) >= 3: s['clr'] *= 0.5
  if s['clr'] < 1e-5: break

print '[BEST DEV-NLL]', best_dev_nll
