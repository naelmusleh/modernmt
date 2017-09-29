import glob
import json
import logging
import math
import time

import os
from torch import nn, torch
from torch.autograd import Variable

from nmmt.torch_utils import torch_is_multi_gpu, torch_is_using_cuda
from onmt import Constants, Optim


class _Stats(object):
    def __init__(self):
        self.start_time = time.time()
        self.total_loss = 0
        self.src_words = 0
        self.tgt_words = 0
        self.num_correct = 0

    def update(self, loss, src_words, tgt_words, num_correct):
        self.total_loss += loss
        self.src_words += src_words
        self.tgt_words += tgt_words
        self.num_correct += num_correct

    @property
    def accuracy(self):
        return float(self.num_correct) / self.tgt_words

    @property
    def loss(self):
        return self.total_loss / self.tgt_words

    @property
    def perplexity(self):
        return math.exp(self.loss)

    def __str__(self):
        elapsed_time = time.time() - self.start_time

        return '[num_correct: %6.2f; %3.0f src tok; %3.0f tgt tok; ' \
               'acc: %6.2f; ppl: %6.2f; %3.0f src tok/s; %3.0f tgt tok/s]' % (
                   self.num_correct, self.src_words, self.tgt_words,
                   self.accuracy * 100, self.perplexity, self.src_words / elapsed_time, self.tgt_words / elapsed_time
               )


class NMTEngineTrainer:
    class Options(object):
        def __init__(self):
            self.log_level = logging.INFO

            self.batch_size = 64
            self.max_generator_batches = 32  # Maximum batches of words in a seq to run the generator on in parallel.

            self.report_steps = 100  # Log status every 'report_steps' steps
            self.validation_steps = 10000  # compute the validation score every 'validation_steps' steps
            self.checkpoint_steps = 10000  # Drop a checkpoint every 'checkpoint_steps' steps
            self.steps_limit = None  # If set, run 'steps_limit' steps at most

            self.optimizer = 'sgd'
            self.learning_rate = 1.
            self.max_grad_norm = 5
            self.lr_decay = 0.9
            self.lr_decay_steps = 10000  # decrease learning rate every 'lr_decay_steps' steps
            self.lr_decay_start_at = 50000  # start learning rate decay after 'start_decay_at' steps

            self.early_stop = 10  # terminate training if validations is stalled for 'early_stop' times
            self.n_avg_checkpoints = 5  # number of checkpoints to merge at the end of training process

        def __str__(self):
            return str(self.__dict__)

        def __repr__(self):
            return str(self.__dict__)

    class State(object):
        def __init__(self, size):
            self.size = size
            self.checkpoint = None
            self.history = []

        def empty(self):
            return len(self.history) == 0

        @property
        def last_step(self):
            return self.checkpoint['step'] if self.checkpoint is not None else 0

        @staticmethod
        def _delete_checkpoint(checkpoint):
            for path in glob.glob(checkpoint['file'] + '.*'):
                os.remove(path)

        def _contains(self, checkpoint):
            for h in self.history:
                if h['file'] == checkpoint['file']:
                    return True
            return False

        def add_checkpoint(self, step, file_path, perplexity):
            if self.checkpoint is not None and not self._contains(self.checkpoint):
                self._delete_checkpoint(self.checkpoint)

            self.checkpoint = {
                'step': step,
                'file': file_path,
                'perplexity': perplexity
            }

            self.history.append(self.checkpoint)
            self.history.sort(key=lambda e: e['perplexity'], reverse=False)

            if len(self.history) > self.size:
                for c in self.history[self.size:]:
                    if c['file'] != file_path:  # do not remove last checkpoint
                        self._delete_checkpoint(c)

                self.history = self.history[:self.size]

        def save_to_file(self, file_path):
            with open(file_path, 'w') as stream:
                stream.write(json.dumps(self.__dict__, indent=4))

        @staticmethod
        def load_from_file(file_path):
            state = NMTEngineTrainer.State(0)
            with open(file_path, 'r') as stream:
                state.__dict__ = json.loads(stream.read())
            return state

    def __init__(self, engine, options=None, optimizer=None, state=None):
        self._logger = logging.getLogger('nmmt.NMTEngineTrainer')
        self._engine = engine
        self.opts = options if options is not None else NMTEngineTrainer.Options()
        self.state = state if state is not None else NMTEngineTrainer.State(self.opts.n_avg_checkpoints)

        if optimizer is None:
            optimizer = Optim(self.opts.optimizer, self.opts.learning_rate, max_grad_norm=self.opts.max_grad_norm,
                              lr_decay=self.opts.lr_decay, lr_start_decay_at=self.opts.lr_decay_start_at)
            optimizer.set_parameters(engine.model.parameters())
        self.optimizer = optimizer

    def reset_learning_rate(self, value):
        self.optimizer.lr = value
        self.optimizer.set_parameters(self._engine.model.parameters())

    def _log(self, message):
        self._logger.log(self.opts.log_level, message)

    @staticmethod
    def _new_nmt_criterion(vocab_size):
        weight = torch.ones(vocab_size)
        weight[Constants.PAD] = 0
        criterion = nn.NLLLoss(weight, size_average=False)
        if torch_is_using_cuda():
            criterion.cuda()
        return criterion

    def _compute_memory_efficient_loss(self, outputs, targets, generator, criterion, evaluation=False):
        # compute generations one piece at a time
        num_correct, loss = 0, 0
        outputs = Variable(outputs.data, requires_grad=(not evaluation), volatile=evaluation)

        batch_size = outputs.size(1)
        outputs_split = torch.split(outputs, self.opts.max_generator_batches)
        targets_split = torch.split(targets, self.opts.max_generator_batches)

        for i, (out_t, targ_t) in enumerate(zip(outputs_split, targets_split)):
            out_t = out_t.view(-1, out_t.size(2))
            scores_t = generator(out_t)
            loss_t = criterion(scores_t, targ_t.view(-1))
            pred_t = scores_t.max(1)[1]
            num_correct_t = pred_t.data.eq(targ_t.data).masked_select(targ_t.ne(Constants.PAD).data).sum()
            num_correct += num_correct_t
            loss += loss_t.data[0]
            if not evaluation:
                loss_t.div(batch_size).backward()

        grad_output = None if outputs.grad is None else outputs.grad.data
        return loss, grad_output, num_correct

    def _evaluate(self, step, criterion, dataset):
        total_loss = 0
        total_words = 0
        total_num_correct = 0

        model = self._engine.model
        model.eval()

        iterator = dataset.iterator(self.opts.batch_size, shuffle=False, volatile=True)

        for _, batch in iterator:
            # exclude original indices
            batch = batch[:-1]
            outputs = model(batch)
            # exclude <s> from targets
            targets = batch[1][1:]
            loss, _, num_correct = self._compute_memory_efficient_loss(outputs, targets, model.generator,
                                                                       criterion, evaluation=True)
            total_loss += loss
            total_num_correct += num_correct
            total_words += targets.data.ne(Constants.PAD).sum()

        model.train()

        valid_loss, valid_acc = total_loss / total_words, float(total_num_correct) / total_words
        valid_ppl = math.exp(min(valid_loss, 100))

        self._log('Validation Set at step %d: loss = %g, perplexity = %g, accuracy = %g' % (
            step, valid_loss, valid_ppl, (float(valid_acc) * 100)))

        return valid_ppl

    def _train_step(self, batch, criterion, stats):
        batch = batch[:-1]  # exclude original indices

        self._engine.model.zero_grad()
        outputs = self._engine.model(batch)
        targets = batch[1][1:]  # exclude <s> from targets
        loss, grad_output, num_correct = self._compute_memory_efficient_loss(outputs, targets,
                                                                             self._engine.model.generator, criterion)
        outputs.backward(grad_output)

        # update the parameters
        self.optimizer.step()

        src_words = batch[0][1].data.sum()
        tgt_words = targets.data.ne(Constants.PAD).sum()

        for stat in stats:
            stat.update(loss, src_words, tgt_words, num_correct)

    def train_model(self, train_dataset, valid_dataset=None, save_path=None):
        state_file_path = None if save_path is None else os.path.join(save_path, 'state.json')

        # set the mask to None; required when the same model is trained after a translation
        if torch_is_multi_gpu():
            decoder = self._engine.model.module.decoder
        else:
            decoder = self._engine.model.decoder
        decoder.attn.applyMask(None)

        self._engine.model.train()

        # define criterion of each GPU
        criterion = self._new_nmt_criterion(self._engine.trg_dict.size())

        step = self.state.last_step
        valid_ppl_best = None
        valid_ppl_stalled = 0  # keep track of how many consecutive validations do not improve the best perplexity

        try:
            checkpoint_stats = _Stats()
            report_stats = _Stats()

            for step, batch in train_dataset.iterator(self.opts.batch_size, loop=True, start_position=step):

                # Terminate policy -------------------------------------------------------------------------------------
                if valid_ppl_stalled >= self.opts.early_stop \
                        or (self.opts.steps_limit is not None and step >= self.opts.steps_limit):
                    break

                # Run step ---------------------------------------------------------------------------------------------
                self._train_step(batch, criterion, [checkpoint_stats, report_stats])
                step += 1

                # Report -----------------------------------------------------------------------------------------------
                if (step % self.opts.report_steps) == 0:
                    self._log('Step %d: %s' % (step, str(report_stats)))
                    report_stats = _Stats()

                if (step % len(train_dataset)) == 0:
                    epoch = int(step / len(train_dataset))
                    self._log('New epoch %d is starting at step %d' % (epoch, step))

                valid_perplexity = None

                # Validation -------------------------------------------------------------------------------------------
                if valid_dataset is not None and (step % self.opts.validation_steps) == 0:
                    valid_perplexity = self._evaluate(step, criterion, valid_dataset)

                    if valid_ppl_best is None or valid_perplexity < valid_ppl_best:
                        valid_ppl_best = valid_perplexity
                        valid_ppl_stalled = 0
                    else:
                        valid_ppl_stalled += 1

                    self._log('Validation perplexity stalled %d times' % valid_ppl_stalled)

                # Learning rate update --------------------------------------------------------------------------------
                if valid_ppl_stalled > 0:  # activate decay only if validation perplexity starts to increase
                    if step > self.optimizer.lr_start_decay_at:
                        if not self.optimizer.lr_start_decay:
                            self._log('Optimizer learning rate decay activated at %d step with decay value %f; '
                                      'current lr value: %f' % (step, self.optimizer.lr_decay, self.optimizer.lr))
                        self.optimizer.lr_start_decay = True

                else:  # otherwise de-activate
                    if self.optimizer.lr_start_decay:
                        self._log('Optimizer learning rate decay de-activated at %d step; current lr value: %f' % (
                            step, self.optimizer.lr))
                    self.optimizer.lr_start_decay = False

                if self.optimizer.lr_start_decay and (step % self.opts.lr_decay_steps) == 0:
                    self.optimizer.updateLearningRate()
                    self._log('Optimizer learning rate after step %d set to lr = %g' % (step, self.optimizer.lr))

                # Checkpoint -------------------------------------------------------------------------------------------
                if (step % self.opts.checkpoint_steps) == 0 and save_path is not None:
                    if valid_perplexity is None and valid_dataset is not None:
                        valid_perplexity = self._evaluate(step, criterion, valid_dataset)

                    checkpoint_ppl = valid_perplexity if valid_perplexity is not None else checkpoint_stats.perplexity
                    checkpoint_file = os.path.join(save_path, 'checkpoint_%d' % step)

                    self._log('Checkpoint at %d: %s' % (step, str(checkpoint_stats)))
                    self._engine.save(checkpoint_file)
                    self.state.add_checkpoint(step, checkpoint_file, checkpoint_ppl)
                    self.state.save_to_file(state_file_path)
                    self._logger.info('Checkpoint saved: path = %s ppl = %.2f' % (checkpoint_file, checkpoint_ppl))

                    checkpoint_stats = _Stats()

        except KeyboardInterrupt:
            pass

        return self.state
