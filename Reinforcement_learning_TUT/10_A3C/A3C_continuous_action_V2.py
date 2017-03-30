"""
Asynchronous Advantage Actor Critic (A3C) with continuous action space, Reinforcement Learning.

The Pendulum example.

View more on [莫烦Python] : https://morvanzhou.github.io/tutorials/

Using:
tensorflow 1.0
gym 0.8.0
"""

import multiprocessing
import threading
import tensorflow as tf
import numpy as np
import gym
import os
import shutil

np.random.seed(2)
tf.set_random_seed(2)  # reproducible

GAME = 'Pendulum-v0'
OUTPUT_GRAPH = True
LOG_DIR = './log'
N_WORKERS = multiprocessing.cpu_count()
MAX_EP_STEP = 500
MAX_GLOBAL_EP = 1000
GLOBAL_NET_SCOPE = 'Global_Net'
UPDATE_GLOBAL_ITER = 5
GAMMA = 0.9
ENTROPY_BETA = 0.005
LR_A = 0.001    # learning rate for actor
LR_C = 0.002    # learning rate for critic

env = gym.make(GAME)

N_S = env.observation_space.shape[0]
N_A = env.action_space.shape[0]
A_BOUND = [env.action_space.low, env.action_space.high]


class ACNet(object):
    def __init__(self, scope, n_s, n_a,
                 a_bound=None, sess=None,
                 opt_a=None, opt_c=None, global_a_params=None, global_c_params=None):

        if scope == GLOBAL_NET_SCOPE:   # get global network
            with tf.variable_scope(scope):
                self.s = tf.placeholder(tf.float32, [None, n_s], 'S')
                self._build_net(n_a)
                self.a_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/actor')
                self.c_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/critic')
        else:   # local net, calculate losses
            self.sess = sess
            with tf.variable_scope(scope):
                self.s = tf.placeholder(tf.float32, [None, n_s], 'S')
                self.a_his = tf.placeholder(tf.float32, [None, n_a], 'A')
                self.v_target = tf.placeholder(tf.float32, [None, 1], 'Vtarget')

                mu, sigma, self.v = self._build_net(n_a)

                td = tf.subtract(self.v_target, self.v, name='TD_error')
                with tf.name_scope('c_loss'):
                    self.c_loss = tf.reduce_sum(tf.square(td))

                with tf.name_scope('wrap_a_out'):
                    mu, sigma = mu * a_bound[1], sigma*2 + 1e-2
                    self.test = sigma[0]

                normal_dist = tf.contrib.distributions.Normal(mu, sigma)

                with tf.name_scope('a_loss'):
                    log_prob = normal_dist.log_prob(self.a_his)
                    exp_v = log_prob * td
                    entropy = normal_dist.entropy()  # encourage exploration
                    self.exp_v = tf.reduce_sum(ENTROPY_BETA * entropy + exp_v)
                    self.a_loss = -self.exp_v

                self.total_loss = self.a_loss + self.c_loss
                with tf.name_scope('choose_a'):  # use local params to choose action
                    self.A = tf.clip_by_value(tf.squeeze(normal_dist.sample(1), axis=0), a_bound[0], a_bound[1])
                with tf.name_scope('local_grad'):
                    self.params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope)
                    self.grads = tf.gradients(self.total_loss, self.a_params)  # get local gradients

            with tf.name_scope('sync'):
                with tf.name_scope('pull'):
                    self.pull_a_params_op = [l_p.assign(g_p) for l_p, g_p in zip(self.a_params, global_a_params)]
                    self.pull_c_params_op = [l_p.assign(g_p) for l_p, g_p in zip(self.c_params, global_c_params)]
                with tf.name_scope('push'):
                    self.update_a_op = opt_a.apply_gradients(zip(self.a_grads, global_a_params))
                    self.update_c_op = opt_c.apply_gradients(zip(self.c_grads, global_c_params))

    def _build_net(self, n_a):
        w_init = tf.random_normal_initializer(0., .1)
        l = tf.layers.dense(self.s, 100, None, kernel_initializer=w_init, name='la')
        mu = tf.layers.dense(l, n_a, tf.nn.tanh, kernel_initializer=w_init, name='mu')
        # control variance, not let it goes too high
        sigma = tf.layers.dense(l, n_a, tf.nn.sigmoid, kernel_initializer=w_init, name='sigma')
        v = tf.layers.dense(l, 1, kernel_initializer=w_init, name='v')  # state value
        return mu, sigma, v

    def update_global(self, feed_dict):  # run by a local
        self.sess.run([self.update_a_op, self.update_c_op], feed_dict)  # local grads applies to global net

    def pull_global(self):  # run by a local
        self.sess.run([self.pull_a_params_op, self.pull_c_params_op])

    def choose_action(self, s):  # run by a local
        s = s[np.newaxis, :]
        return self.sess.run(self.A, {self.s: s})[0]


class Worker(object):
    def __init__(self, env, name, n_s, n_a, a_bound, sess, opt_a, opt_c, g_a_params, g_c_params):
        self.env = env
        self.sess = sess
        self.name = name
        self.AC = ACNet(name, n_s, n_a, a_bound, sess, opt_a, opt_c, g_a_params, g_c_params)

    def work(self, update_iter, max_ep_step, gamma, coord):
        total_step = 1
        buffer_s, buffer_a, buffer_r = [], [], []
        while not coord.should_stop() and GLOBAL_EP.eval(self.sess) < MAX_GLOBAL_EP:
            s = self.env.reset()
            ep_r = 0
            for ep_t in range(max_ep_step):
                if self.name == 'W_0':
                    self.env.render()
                a = self.AC.choose_action(s)
                s_, r, done, info = self.env.step(a)
                r /= 10     # normalize reward
                ep_r += r
                buffer_s.append(s)
                buffer_a.append(a)
                buffer_r.append(r)

                if total_step % update_iter == 0 or done:   # update global and assign to local net
                    if done:
                        v_s_ = 0   # terminal
                    else:
                        v_s_ = self.sess.run(self.AC.v, {self.AC.s: s_[np.newaxis, :]})[0, 0]
                    buffer_v_target = []
                    for r in buffer_r[::-1]:    # reverse buffer r
                        v_s_ = r + gamma * v_s_
                        buffer_v_target.append(v_s_)
                    buffer_v_target.reverse()

                    buffer_s, buffer_a, buffer_v_target = np.vstack(buffer_s), np.vstack(buffer_a), np.vstack(buffer_v_target)
                    feed_dict = {
                        self.AC.s: buffer_s,
                        self.AC.a_his: buffer_a,
                        self.AC.v_target: buffer_v_target,
                    }
                    self.AC.update_global(feed_dict)
                    buffer_s, buffer_a, buffer_r = [], [], []
                    self.AC.pull_global()

                s = s_
                total_step += 1
                if ep_t == max_ep_step-1:
                    print(
                        self.name,
                        "Ep:", GLOBAL_EP.eval(self.sess),
                        "| Ep_r: %.2f" % ep_r,
                          )
                    sess.run(COUNT_GLOBAL_EP)
                    break

if __name__ == "__main__":
    sess = tf.Session()

    with tf.device("/cpu:0"):
        GLOBAL_EP = tf.Variable(0, dtype=tf.int32, name='global_ep', trainable=False)
        COUNT_GLOBAL_EP = tf.assign(GLOBAL_EP, tf.add(GLOBAL_EP, tf.constant(1), name='step_ep'))
        OPT_A = tf.train.RMSPropOptimizer(LR_A, name='RMSPropA')
        OPT_C = tf.train.RMSPropOptimizer(LR_C, name='RMSPropC')
        globalAC = ACNet(GLOBAL_NET_SCOPE, N_S, N_A)  # we only need its params
        workers = []
        # Create worker
        for i in range(N_WORKERS):
            i_name = 'W_%i' % i   # worker name
            workers.append(
                Worker(
                    gym.make(GAME).unwrapped, i_name, N_S, N_A, A_BOUND, sess,
                    OPT_A, OPT_C, globalAC.a_params, globalAC.c_params
                ))

    coord = tf.train.Coordinator()
    sess.run(tf.global_variables_initializer())

    if OUTPUT_GRAPH:
        if os.path.exists(LOG_DIR):
            shutil.rmtree(LOG_DIR)
        tf.summary.FileWriter(LOG_DIR, sess.graph)

    worker_threads = []
    for worker in workers:
        job = lambda: worker.work(UPDATE_GLOBAL_ITER, MAX_EP_STEP, GAMMA, coord)
        t = threading.Thread(target=job)
        t.start()
        worker_threads.append(t)
    coord.join(worker_threads)