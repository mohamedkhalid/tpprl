import tensorflow as tf
import numpy as np
import datetime as D
import matplotlib.pyplot as plt
import seaborn as sns


def variable_summaries(var, name=None):
    """Attach a lot of summaries to a Tensor (for TensorBoard visualization)."""
    if name is None:
        name = var.name.split('/')[-1][:-2]

    with tf.name_scope('summaries-' + name):
        mean = tf.reduce_mean(var)
        tf.summary.scalar('mean', mean)
        with tf.name_scope('stddev'):
            stddev = tf.sqrt(tf.reduce_mean(tf.square(var - mean)))
        tf.summary.scalar('stddev', stddev)
        tf.summary.scalar('max', tf.reduce_max(var))
        tf.summary.scalar('min', tf.reduce_min(var))
        tf.summary.histogram('histogram', var)


def _now(raw=False):
    """Return the time now in red color."""
    templ = '\x1b[31m[{}]\x1b[0m' if not raw else '{}'
    return (templ
            .format(D.datetime.now()
                    .isoformat(timespec='seconds')))


def average_gradients(tower_grads):
    """Calculate the average gradient for each shared variable across all towers.
    Note that this function provides a synchronization point across all towers.
    Args:
        tower_grads: List of lists of (gradient, variable) tuples. The outer list
            is over individual gradients. The inner list is over the gradient
            calculation for each tower.
    Returns:
         List of pairs of (gradient, variable) where the gradient has been averaged
         across all towers.
    """
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        # Note that each grad_and_vars looks like the following:
        #     ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
        grads = []
        for g, _ in grad_and_vars:
            # Add 0 dimension to the gradients to represent the tower.
            expanded_g = tf.expand_dims(g, 0)

            # Append on a 'tower' dimension which we will average over below.
            grads.append(expanded_g)

        # Average over the 'tower' dimension.
        grad = tf.concat(axis=0, values=grads)
        grad = tf.reduce_mean(grad, 0)

        # Keep in mind that the Variables are redundant because they are shared
        # across towers. So .. we will just return the first tower's pointer to
        # the Variable.
        v = grad_and_vars[0][1]
        grad_and_var = (grad, v)
        average_grads.append(grad_and_var)
    return average_grads


def get_test_perf(trainer, seeds, t_min, t_max):
    """Takes the trainer and performs simulations for the given set of seeds."""

    seeds = list(seeds)

    dfs = [trainer.run_sim(seed) for seed in seeds]
    f_d = trainer.get_feed_dict(dfs, is_test=True)
    h_states = trainer.sess.run(trainer.h_states, feed_dict=f_d)

    times = np.arange(t_min, t_max, (t_max - t_min) / 5000)
    return trainer.calc_u(h_states=h_states, feed_dict=f_d,
                          batch_size=len(seeds), times=times)


def plot_u(times, u, t_deltas, is_own_event, figsize=(16, 6)):
    """Plots the intensity output by our broadcaster.

    TODO: May not work if the max_events are reached.
    """

    t_deltas = np.asarray(t_deltas)
    is_own_event = np.asarray(is_own_event)

    seq_len = np.nonzero(t_deltas == 0)[0][0]  # First index where t_delta = 0
    abs_t = np.cumsum(t_deltas[:seq_len])
    abs_own = is_own_event[:seq_len]

    our_events = [t for (t, o) in zip(abs_t, abs_own) if o]
    other_events = [t for (t, o) in zip(abs_t, abs_own) if not o]

    u_max = np.max(u)

    plt.figure(figsize=(16, 6))

    c1, c2, c3 = sns.color_palette(n_colors=3)

    plt.plot(times, u, label='$u(t)$', color=c1)
    plt.vlines(our_events, 0, 0.75 * u_max, label='Us', alpha=0.5, color=c2)
    plt.vlines(other_events, 0, 0.75 * u_max, label='Others', alpha=0.5, color=c3)
    plt.xlabel('Time')
    plt.ylabel('$u(t)$')
    plt.legend()