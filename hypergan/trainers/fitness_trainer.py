import tensorflow as tf
import numpy as np
import hyperchamber as hc
import inspect

from hypergan.trainers.base_trainer import BaseTrainer

TINY = 1e-12

class FitnessTrainer(BaseTrainer):
    def create(self):
        self.hist = [0 for i in range(2)]
        self.steps_since_fit=0
        self.old_fitness = None
        config = self.config
        lr = config.learn_rate
        self.global_step = tf.train.get_global_step()
        self.mix_threshold_reached = False
        decay_function = config.decay_function
        if decay_function:
            print("!!using decay function", decay_function)
            decay_steps = config.decay_steps or 50000
            decay_rate = config.decay_rate or 0.9
            decay_staircase = config.decay_staircase or False
            self.lr = decay_function(lr, self.global_step, decay_steps, decay_rate, decay_staircase)
        else:
            self.lr = lr

        return self._create()


    def _create(self):
        gan = self.gan
        config = self.config

        d_vars = self.d_vars or gan.discriminator.variables()
        g_vars = self.g_vars or (gan.encoder.variables() + gan.generator.variables())
        self.prev_zs = []

        loss = self.loss or gan.loss
        d_loss, g_loss = loss.sample
        def random_like(x):
            shape = self.ops.shape(x)
            return tf.random_uniform(shape, minval=-0.1, maxval=0.1)
        prev_sample = tf.Variable(random_like(gan.generator.sample), dtype=tf.float32)
        self.prev_sample = prev_sample
        self.update_prev_sample = tf.assign(prev_sample, gan.generator.sample)
        self.prev_l2_loss = (self.config.prev_l2_loss_lambda or 0.1)*self.ops.squash(tf.square(gan.generator.sample-prev_sample))
        gan.metrics['prev_l2']=self.prev_l2_loss

        self.l2_loss = g_loss + self.prev_l2_loss

        allloss = d_loss + g_loss

        allvars = d_vars + g_vars

        d_grads = tf.gradients(d_loss, d_vars)
        if config.prev_l2_loss:
            g_grads = tf.gradients(self.l2_loss, g_vars)
        else:
            g_grads = tf.gradients(g_loss, g_vars)


        grads = d_grads + g_grads

        self.d_log = -tf.log(tf.abs(d_loss+TINY))
        for g, d_v in zip(grads,d_vars):
            if g is None:
                print("!!missing gradient")
                print(d_v)
                return
        reg = 0.5 * sum(
            tf.reduce_sum(tf.square(g)) for g in grads if g is not None
        )
        if config.update_rule == "ttur" or config.update_rule == 'single-step':
            Jgrads = [0 for i in allvars]
        else:
            Jgrads = tf.gradients(reg, allvars)

        self.g_gradient = tf.ones([1])
        def amp_for(v):
            if v in g_vars:
                return config.g_w_lambda or 3
            if v in d_vars:
                return config.d_w_lambda or 1

        def applyvec(g, jg, v):
            print("JG ALPHA", config.jg_alpha)
            nextw = g + jg * (config.jg_alpha or 0.1)
            return nextw

        def gradient_for(g, jg, v):
            def _gradient():
                if config.update_rule == 'single-step':
                    return g
                elif config.update_rule == "ttur":
                    ng = amp_for(v)*g
                else:
                    ng = applyvec(g, jg, v)
                return ng
            ng = _gradient()
            return ng
        apply_vec = []
        apply_vec_d = []
        apply_vec_g = []
        for (i, g, Jg, v) in zip(range(len(grads)), grads, Jgrads, allvars): 
            if Jg is not None:
                gradient = gradient_for(g, Jg, v)
                print("Applying gradient", gradient)
                apply_vec.append((gradient, v))
                if i < len(d_vars):
                    apply_vec_d.append((gradient, v))
                else:
                    apply_vec_g.append((gradient, v))

        defn = {k: v for k, v in config.items() if k in inspect.getargspec(config.trainer).args}
        tr = config.trainer(self.lr, **defn)


        optimizer = tr.apply_gradients(apply_vec, global_step=self.global_step)
        d_optimizer = tr.apply_gradients(apply_vec_d, global_step=self.global_step)
        g_optimizer = tr.apply_gradients(apply_vec_g, global_step=self.global_step)

        def _update_ortho(v,i):
            if len(v.shape) == 4:
                identity = tf.cast(tf.diag(np.ones(self.ops.shape(v)[0])), tf.float32)
                v_transpose = tf.transpose(v, perm=[0,1,3,2])
                #s = self.ops.shape(v_transpose)
                #identity = tf.reshape(identity, [s[0],s[1],1,1])
                #identity = tf.tile(identity, [1,1,s[2],s[3]])
                decay = self.config.ortho_decay or 0.01
                newv = tf.matmul(v, tf.matmul(v_transpose,v))
                newv=(1+decay)*v - decay*(newv)

                #newv = tf.transpose(v, perm=[1,0,2,3])
                return tf.assign(v, newv)
            return None
        def _update_lipschitz(v,i):
            if len(v.shape) > 1:
                k = config.weight_constraint_k or 100.0000
                wi_hat = v
                if len(v.shape) == 4:
                    #fij = tf.reduce_sum(tf.abs(wi_hat),  axis=[0,1])
                    fij = wi_hat
                    fij = tf.reduce_sum(tf.abs(fij),  axis=[1])
                    fij = tf.reduce_max(fij,  axis=[0])
                else:
                    fij = wi_hat

                if self.config.ortho_pnorm == "inf":
                    wp = tf.reduce_max(tf.reduce_sum(tf.abs(fij), axis=0), axis=0)
                else:
                    # conv
                    wp = tf.reduce_max(tf.reduce_sum(tf.abs(fij), axis=1), axis=0)
                ratio = (1.0/tf.maximum(1.0, wp/k))
                
                if config.weight_bounce:
                    bounce = tf.minimum(1.0, tf.ceil(wp/k-0.999))
                    ratio -= tf.maximum(0.0, bounce) * 0.2

                if config.weight_scaleup:
                    up = tf.minimum(1.0, tf.ceil(0.02-wp/k))
                    ratio += tf.maximum(0.0, up) * k/wp * 0.2

                print('--',i,v)
                wi = ratio*(wi_hat)
                #self.gan.metrics['wi'+str(i)]=wp
                #self.gan.metrics['wk'+str(i)]=ratio
                #self.gan.metrics['bouce'+str(i)]=bounce
                return tf.assign(v, wi)
            return None

        def _update_l2nn(v,i):
            if len(v.shape) > 1:
                w=v
                w = tf.reduce_sum(tf.abs(w),  axis=[1])
                w = tf.reduce_max(w,  axis=[0])
                wt = tf.transpose(w)
                #wt = tf.transpose(w, perm=[1,0,2,3])
                def _r(m):
                    s = self.ops.shape(m)
                    m = tf.abs(m)
                    m = tf.reduce_sum(m, axis=1,keep_dims=True)
                    m = tf.reduce_max(m, axis=0,keep_dims=True)
                    #m = tf.tile(m,[s[0],s[1],1,1])
                    return m
                wtw = tf.matmul(wt,w)
                wwt = tf.matmul(w,wt)
                bw = tf.minimum(_r(wtw), _r(wwt))
                print("BW", bw, w, _r(wtw), wtw, wt)
                decay = self.config.l2nn_decay or 0.0001
                wi = (v/tf.sqrt(bw))
                wi = (1-decay)*v+(decay*wi) # [3,3,128,256] / [128, 256]
                #self.gan.metrics['l2nn'+str(i)]=self.ops.squash(wi)
                return tf.assign(v, wi)
            return None

        def _update_weight_constraint(v,i):
            #skipped = [gan.generator.ops.weights[0], gan.generator.ops.weights[-1], gan.discriminator.ops.weights[0], gan.discriminator.ops.weights[-1]]
            skipped = [gan.discriminator.ops.weights[-1]]
            for skip in skipped:
                if self.ops.shape(v) == self.ops.shape(skip):
                    print("SKIPPIG", v)
                    return None
            constraints = config.weight_constraint or []
            result = []
            if "ortho" in constraints:
                result.append(_update_ortho(v,i))
            if "lipschitz" in constraints:
                result.append(_update_lipschitz(v,i))
            if "l2nn" in constraints:
                result.append(_update_l2nn(v,i))
            result = [r for r in result if r is not None]
            return tf.group(result)
        self.past_weights = []

        for v in allvars:
            self.past_weights.append(tf.Variable(v, dtype=tf.float32))
        #self.update_weight_constraints = [_update_weight_constraint(v,i) for i,v in enumerate(allvars)]
        self.update_weight_constraints = [_update_weight_constraint(v,i) for i,v in enumerate(allvars + self.past_weights)]
        self.update_weight_constraints = [v for v in self.update_weight_constraints if v is not None]
        print('UPDATE_WEIGHT', self.update_weight_constraints)


        def _ema(v, pastv):
            decay = config.ema_decay
            if decay is None:
                decay = 0.9
            return tf.assign(v, v*(1-decay)+pastv*decay)
        self.assign_ema = tf.group([_ema(a,b) for a,b in zip(allvars, self.past_weights)])
        self.assign_past_weights = tf.group([tf.assign(b,a) for a,b in zip(allvars, self.past_weights)])

        self.g_loss = g_loss
        self.d_loss = d_loss
        self.slot_vars = tr.variables()

            
        def _slot_var(x, g_vars):
            for g in g_vars:
                if x.name.startswith(g.name.split(":")[0]):
                    return True
            return False
        self.slot_vars_g = [x for x in self.slot_vars if _slot_var(x, g_vars)]
        self.slot_vars_d = [x for x in self.slot_vars if _slot_var(x, d_vars)]

        self.optimizer = optimizer
        self.d_optimizer = d_optimizer
        self.g_optimizer = g_optimizer
        self.min_fitness=None
        
        if config.fitness_test is not None:
            mean = tf.zeros([1])
            used_grads = d_grads
            if config.grad_type == "sum":
                for g in used_grads:
                    mean += tf.reduce_sum(tf.abs(g))
            else:
                for g in used_grads:
                    mean += tf.reduce_mean(tf.abs(g))
                mean/=len(used_grads)
            self.mean=mean
            #self.mean=mean*100
            if config.fitness_type == 'g_loss':
                self.g_fitness = g_loss - (config.diversity_importance or 1) * tf.log(tf.abs(self.mean + d_loss - g_loss))
            elif(config.fitness_type == 'gradient-only'):
                self.g_fitness = -tf.log(reg)
            elif(config.fitness_type == 'grads'):
                self.g_fitness = mean
            elif(config.fitness_type == 'point'):
                self.g_fitness = mean - 1000*d_loss + 1000*g_loss
            elif(config.fitness_type == 'fail'):
                self.g_fitness = -mean
            elif(config.fitness_type == 'fail2'):
                self.g_fitness = -loss.d_fake
            elif(config.fitness_type == 'fail3'):
                self.g_fitness = -g_loss
            elif(config.fitness_type == 'fail2-reverse'):
                self.g_fitness = loss.d_fake
            elif(config.fitness_type == 'ls'):
                a,b,c = loss.config.labels
                self.g_fitness = tf.square(loss.d_fake-a)
            elif(config.fitness_type == 'ls-r'):
                a,b,c = loss.config.labels
                self.g_fitness = -tf.square(loss.d_fake-a)
            elif(config.fitness_type == 'ls2'):
                a,b,c = loss.config.labels
                self.g_fitness = tf.square(loss.d_fake-c)
            elif(config.fitness_type == 'ls2-r'):
                a,b,c = loss.config.labels
                self.g_fitness = -tf.square(loss.d_fake-c)
            elif(config.fitness_type == 'std'):
                self.g_fitness = -tf.nn.sigmoid(loss.d_fake)
            elif(config.fitness_type == 'ls3'):
                self.g_fitness = 1-loss.d_fake
            elif(config.fitness_type == 'ls4'):
                self.g_fitness = loss.d_real-loss.d_fake
            elif(config.fitness_type == 'ls5'):
                self.g_fitness = tf.square(loss.d_real)-tf.square(loss.d_fake)
            elif(config.fitness_type == 'fq1'):
                lam = 0.1
                self.g_fitness = -loss.d_fake-lam*mean
            elif(config.fitness_type == 'fq2'):
                lam = 0.1
                self.g_fitness = loss.d_real-loss.d_fake-lam*mean
            elif(config.fitness_type == 'fq3'):
                lam = 1
                self.g_fitness = loss.d_real-loss.d_fake+lam*mean
            elif(config.fitness_type == 'fq4'):
                lam = 1
                self.g_fitness = -loss.d_fake+lam*mean
            elif(config.fitness_type == 'fq5'):
                lam = 1
                self.g_fitness = -loss.d_fake-lam*tf.norm(mean)
            elif(config.fitness_type == 'fq6'):
                lam = 0.1
                self.g_fitness = -loss.d_fake-lam*tf.norm(mean+d_loss)
            elif(config.fitness_type == 'fq7'):
                lam = 0.1
                self.g_fitness = -loss.d_fake-lam*tf.norm(-mean-d_loss)
            elif(config.fitness_type == 'fq8'):
                lam = 0.1
                self.g_fitness = -tf.norm(mean+d_loss)
            elif(config.fitness_type == 'fq9'):
                lam = 0.1
                self.g_fitness = lam*mean
            elif(config.fitness_type == 'fq10'):
                lam = 0.1
                self.g_fitness = tf.norm(mean+d_loss)
            elif(config.fitness_type == 'fq11'):
                lam = 100.00
                self.fq = -loss.d_fake
                self.fd = lam * mean
                self.g_fitness = -loss.d_fake + lam * mean
            elif(config.fitness_type == 'ls3-fail'):
                self.g_fitness = -(1-loss.d_fake)
            elif(config.fitness_type == 'gldl'):
                self.g_fitness = -d_loss + g_loss
            elif(config.fitness_type == 'df'):
                self.g_fitness = tf.abs(loss.d_fake) - tf.abs(loss.d_real)
            elif(config.fitness_type == 'standard'):
                self.g_fitness = tf.reduce_mean(g_loss) - (config.diversity_importance or 1)* tf.log(tf.abs(self.mean - tf.log(TINY+tf.sigmoid(d_loss)) - \
                        tf.log(1.0-tf.sigmoid(g_loss)+TINY)))
            else:
                self.g_fitness = tf.reduce_mean(loss.d_fake) - (config.diversity_importance or 1)* tf.log(tf.abs(self.mean + tf.reduce_mean(loss.d_real) - tf.reduce_mean(loss.d_fake)))
            self.g_fitness = tf.reduce_mean(self.g_fitness)

        return optimizer, optimizer

    def required(self):
        return "trainer learn_rate".split()

    def _step(self, feed_dict):
        gan = self.gan
        sess = gan.session
        config = self.config
        loss = self.loss or gan.loss
        metrics = loss.metrics

        if self.current_step == 0 and self.steps_since_fit == 0:
                sess.run(self.assign_past_weights)
                sess.run(self.update_prev_sample)
        
        if config.fitness_test is not None:
            self.steps_since_fit+=1
            if config.fitness_failure_threshold and self.steps_since_fit > (config.fitness_failure_threshold or 1000):
                print("Fitness achieved.", self.hist[0], self.min_fitness)
                self.min_fitness =  None
                self.mix_threshold_reached = True
                self.steps_since_fit = 0
                return
            if self.min_fitness is not None and np.isnan(self.min_fitness):
                print("NAN min fitness")
                self.min_fitness=None
                return
            
            gl, dl, fitness,mean, *zs = sess.run([self.g_loss, self.d_loss, self.g_fitness, self.mean]+gan.fitness_inputs())
            if np.isnan(fitness) or np.isnan(gl) or np.isnan(dl):
                print("NAN Detected.  Candidate done")
                self.min_fitness = None
                self.mix_threshold_reached = True
                self.steps_since_fit = 0
                return
            if self.old_fitness == fitness:
                print("Stuck state detected, unsticking")
                self.min_fitness = None
                return
            self.old_fitness = fitness


            g = None
            if self.config.skip_fitness:
                self.min_fitness = None
            if(self.min_fitness is None or fitness <= self.min_fitness):
                self.hist[0]+=1
                self.min_fitness = fitness
                self.steps_since_fit=0
                if config.assert_similarity:
                    if((gl - dl) > ((config.similarity_ratio or 1.8) * ( (gl + dl) / 2.0)) ):
                        print("g_loss - d_loss > allowed similarity threshold", gl, dl, gl-dl)
                        self.min_fitness = None
                        self.mix_threshold_reached = True
                        self.steps_since_fit = 0
                        return


                for v, t in ([[gl, self.g_loss],[dl, self.d_loss],[fitness, self.g_fitness]] + [ [v, t] for v, t in zip(zs, gan.fitness_inputs())]):
                    feed_dict[t]=v
                # assign prev sample for previous z
                # replace previous z with new z
                prev_feed_dict = {}
                for v, t in ( [ [v, t] for v, t in zip(self.prev_zs, gan.fitness_inputs())]):
                    prev_feed_dict[t]=v

                # l2 = ||(pg(z0) - g(z0))||2
                prev_l2_loss = sess.run(self.prev_l2_loss, prev_feed_dict)
                # pg(z0) = g(z)
                self.prev_g = sess.run(self.update_prev_sample, feed_dict)
                # z0 = z
                self.prev_zs = zs
                # optimize(l2, gl, dl)

                feed_dict[self.prev_l2_loss] = prev_l2_loss

                _, *metric_values = sess.run([self.optimizer] + self.output_variables(metrics), feed_dict)
                if ((self.current_step % (self.config.constraint_every or 100)) == 0):
                    if self.config.weight_constraint:
                        sess.run(self.update_weight_constraints, feed_dict)
                sess.run(self.assign_ema)
                sess.run(self.assign_past_weights)
            else:
                self.hist[1]+=1
                fitness_decay = config.fitness_decay or 0.99
                self.min_fitness = self.min_fitness + (1.00-fitness_decay)*(fitness-self.min_fitness)
                if(config.train_d_on_fitness_failure):
                    metric_values = sess.run([self.d_optimizer]+self.output_variables(metrics), feed_dict)[1:]
                else:
                    metric_values = sess.run(self.output_variables(metrics), feed_dict)
                self.current_step-=1
        else:
            if ((self.current_step % (self.config.constraint_every or 100)) == 0):
                if self.config.weight_constraint:
                    print("Updating constraints")
                    sess.run(self.update_weight_constraints, feed_dict)
            #standard
            gl, dl, *metric_values = sess.run([self.g_loss, self.d_loss, self.optimizer] + self.output_variables(metrics), feed_dict)[1:]
            sess.run(self.assign_ema)
            sess.run(self.assign_past_weights)
            if(gl == 0 or dl == 0):
                self.steps_since_fit=0
                self.mix_threshold_reached = True
                print("Zero, lne?")
                return
            self.steps_since_fit=0

        if ((self.current_step % 10) == 0 and self.steps_since_fit == 0):
            hist_output = "  " + "".join(["G"+str(i)+":"+str(v)+" "for i, v in enumerate(self.hist)])
            print(str(self.output_string(metrics) % tuple([self.current_step] + metric_values)+hist_output))
            self.hist = [0 for i in range(2)]
