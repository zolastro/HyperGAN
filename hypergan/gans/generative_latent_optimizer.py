import importlib
import json
import numpy as np
import os
import sys
import time
import uuid
import copy

from hypergan.discriminators import *
from hypergan.encoders import *
from hypergan.generators import *
from hypergan.inputs import *
from hypergan.samplers import *
from hypergan.trainers import *

import hyperchamber as hc
from hyperchamber import Config
from hypergan.ops import TensorflowOps
import tensorflow as tf
import hypergan as hg

from hypergan.gan_component import ValidationException, GANComponent
from .base_gan import BaseGAN

from hypergan.trainers.multi_step_trainer import MultiStepTrainer

class GenerativeLatentOptimizer(BaseGAN):
    """ 
    GenerativeLatentOptimizer is a network where `z` is trainable and there is no discriminator.

    It's not a GAN at all, but is still useful to base off BaseGAN.
    """
    def __init__(self, *args, **kwargs):
        BaseGAN.__init__(self, *args, **kwargs)
        self.generator = None
        self.loss = None
        self.trainer = None
        self.session = None
        self.zs = {}

    def required(self):
        return "generator".split()

    def create(self):
        BaseGAN.create(self)
        config = self.config

        def create_if(obj):
            if(hasattr(obj, 'create')):
                obj.create()

        with tf.device(self.device):
            if self.session is None: 
                self.session = self.ops.new_session(self.ops_config)

            shape = [self.batch_size(), 4, 4, self.config.encoder.z]
            z = self.ops.get_weight(shape, name='z')

            self.encoder = hc.Config({"sample": z, "z": z}) 

            if self.generator is None and config.generator:
                self.generator = self.create_component(config.generator)
                create_if(self.generator)
                self.uniform_sample = self.generator.sample


            var_lists = [self.variables() + self.generator.variables()]
        
            losses = []
            metrics = []

            if config.type == 'reconstruction':
                self.loss = tf.square(self.inputs.x - self.generator.sample)
            elif config.type == 'pyramid_reconstruction':
                start = [1, 1]
                end = self.ops.shape(self.inputs.x)
                end = [end[1], end[2]]
                current = start
                self.loss = tf.zeros(1)
                while current[0] < start[0] and current[1] < start[0]:
                    current[0]*=2
                    current[1]*=2 # TODO ratio

                    currentx = tf.image.resize_images(self.inputs.x,current, 1)
                    currentg = tf.image.resize_images(self.generator.sample,current, 1)
                    self.loss += self.ops.squash(tf.square(currentx - currentg))

                self.loss += self.ops.squash(tf.square(self.inputs.x - self.generator.sample))
            elif config.type == 'z_encoder':
                encoder_discriminator = self.create_component(config.z_discriminator)
                encoder_discriminator.ops.describe("z_discriminator")
                z_target = tf.random_uniform(self.ops.shape(self.encoder.sample), -1, 1, dtype=tf.float32)
                encoder_discriminator.create(x=z_target, g=self.encoder.sample)

                encoder_loss = self.create_component(self.config.loss, discriminator = encoder_discriminator)
                encoder_loss.create()
                print("D_LOSS", encoder_loss.d_loss)
                with tf.variable_scope("d"):
                    discriminator = self.create_component(config.discriminator)
                    discriminator.create(x=self.inputs.x, g=self.generator.sample)
                standard_loss = self.create_component(self.config.loss, discriminator = discriminator)
                standard_loss.create()
                #self.loss = tf.square(self.inputs.x - self.generator.sample) + encoder_loss.g_loss
                self.loss = encoder_loss.g_loss + standard_loss.g_loss
                self.loss += tf.square(self.inputs.x - self.generator.sample) 
                #reshaped_z = self.ops.reshape(z, self.ops.shape(dx))
                #self.loss += tf.norm(dx - dg)# + tf.norm(dg - reshaped_z)
                #var_lists.append(discriminator.variables() + self.variables())

                var_lists.append(discriminator.variables())
                var_lists.append(encoder_discriminator.variables() + self.variables())
            
                #losses.append(('discriminator', tf.square(dx - reshaped_z)))
                losses.append(('discriminator', standard_loss.d_loss))
                losses.append(('discriminator', encoder_loss.d_loss))
                metrics.append(encoder_loss.metrics)
                metrics.append(encoder_loss.metrics)
                metrics.append(encoder_loss.metrics)
            elif config.type == 'wgan':
                with tf.variable_scope("d"):
                    discriminator = self.create_component(config.discriminator)
                    dx = discriminator.create(net=self.inputs.x)
                    dg = discriminator.reuse(net=self.generator.sample)
                self.loss = tf.abs(0.5 - dx - dg)
                var_lists[0] += discriminator.variables()
                self.loss += 0.01*self.ops.squash(tf.abs(self.inputs.x - self.generator.sample))

            losses = [('generator', self.loss)] + losses
            self.trainer = MultiStepTrainer(self, self.config.trainer, losses, var_lists=var_lists, metrics=metrics)
            self.trainer.create()

            # Hack to assign z to a variable.  Can't just feed z in due to adam error.
            self.assign_z_feed = tf.zeros_like(z)
            self.assign_z = z.assign(self.assign_z_feed)
            self.session.run(tf.global_variables_initializer())

    def set_z(self, filenames, zs):
        for i,_ in enumerate(filenames):
            self.zs[filenames[i]] = zs[i]

    def lookup_z(self, next_filenames):
        zs = []
        for filename in next_filenames:
            shape = self.ops.shape(self.encoder.sample)
            shape[0]=1
            if filename in self.zs:
                z = self.zs[filename]
                z = np.reshape(z, shape)
            else:
                z = np.random.uniform(-1,1,shape)
            zs.append(z)
        stacked = np.vstack(zs)
        return stacked

    def step(self, feed_dict={}):
        if not self.created:
            self.create()
        if self.trainer == None:
            raise ValidationException("gan.trainer is missing.  Cannot train.")
        next_filenames, next_x = self.session.run([self.inputs.filename, self.inputs.x])
        next_z = self.lookup_z(next_filenames)
        feed_dict[self.inputs.x]=next_x
        self.session.run(self.assign_z, {self.assign_z_feed: next_z})

        self.trainer.step(feed_dict)
        
        updated_z = self.session.run(self.encoder.sample, {self.inputs.x: next_x})
        self.set_z(next_filenames, updated_z)

