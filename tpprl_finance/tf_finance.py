# TODO in read_raw_data, return start_time and T
import sys
import pandas as pd
import numpy as np
import multiprocessing as MP

np.set_printoptions(precision=6)
import os
import tensorflow as tf

from util_finance import _now, variable_summaries
from cell_finance import TPPRExpMarkedCellStacked_finance

SAVE_DIR = "/NL/tpprl-result/work/rl-finance/"
# SAVE_DIR = "/home/supriya/MY_HOME/MPI-SWS/dataset"
HIDDEN_LAYER_DIM = 8
MAX_AMT = 1000.0
MAX_SHARE = 100
BASE_CHARGES = 1.0
PERCENTAGE_CHARGES = 0.001
EPSILON = 1e-6
TYPES_OF_PORTFOLIOS = 1


class Action:
    def __init__(self, alpha, n):
        self.alpha = alpha
        self.n = n

    def __str__(self):
        return "<{}: {}>".format("Sell" if self.alpha > 0 else "Buy", self.n)


class Feedback:
    def __init__(self, t_i, v_curr, is_trade_feedback, event_curr_amt, portfolio):
        self.t_i = t_i
        self.v_curr = v_curr
        self.is_trade_feedback = is_trade_feedback
        self.event_curr_amt = event_curr_amt
        self.portfolio = portfolio

    def is_trade_event(self):
        return self.is_trade_feedback == 1

    def is_tick_event(self):
        return self.is_trade_feedback == 2


class TradeFeedback(Feedback):
    def __init__(self, t_i, v_curr, alpha_i, n_i, event_curr_amt, portfolio):
        super(TradeFeedback, self).__init__(t_i=t_i, v_curr=v_curr, is_trade_feedback=1, event_curr_amt=event_curr_amt,
                                            portfolio=portfolio)
        self.alpha_i = alpha_i
        self.n_i = n_i


class TickFeedback(Feedback):
    def __init__(self, t_i, v_curr, event_curr_amt, portfolio):
        super(TickFeedback, self).__init__(t_i=t_i, v_curr=v_curr, is_trade_feedback=2, event_curr_amt=event_curr_amt,
                                           portfolio=portfolio)
        self.alpha_i = 0
        self.n_i = 0


# class State:
#     def __init__(self, curr_time):
#         self.time = curr_time
#         self.events = []
#
#     def apply_event(self, event):
#         self.events.append(event)
#         self.time = event.t_i
#         # if event.alpha_i == 0:
#         #     print("* BUY {} shares at price of {} at time {}".format(event.n_i, event.v_curr, event.t_i))
#         # else:
#         #     print("* SELL {} shares at price of {} at time {}".format(event.n_i, event.v_curr, event.t_i))
#
#     def get_dataframe(self, output_file):
#         df = pd.DataFrame.from_records(
#             [{"t_i": event.t_i,
#               "alpha_i": event.alpha_i,
#               "n_i": event.n_i,
#               "v_curr": event.v_curr,
#               "is_trade_feedback": event.is_trade_feedback,
#               "event_curr_amt": event.event_curr_amt} for event in self.events])
#         print("\n saving events:")
#         print(df[:2].values)
#         df.to_csv(SAVE_DIR + output_file, index=False)
#         return df


class Strategy:
    def __init__(self):
        self.current_amt = MAX_AMT
        self.owned_shares = 0

    def get_next_action_time(self, event):
        return NotImplemented

    def get_next_action_item(self, event):
        return NotImplemented

    def update_owned_shares(self, event):
        prev_amt = self.current_amt
        if event.alpha_i == 0:
            self.owned_shares += event.n_i
            # self.current_amt -= event.n_i * event.v_curr
        elif event.alpha_i == 1:
            self.owned_shares -= event.n_i
            # self.current_amt += event.n_i * event.v_curr

        assert self.current_amt > 0


class RLStrategy(Strategy):
    def __init__(self, wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s,
                 W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h,
                 Vt_h, Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s, RS):
        super(RLStrategy, self).__init__()
        self.RS = RS  # np.random.RandomState(seed)
        self.wt = wt
        self.W_t = W_t
        self.Wb_alpha = Wb_alpha
        self.Ws_alpha = Ws_alpha
        self.Wn_b = Wn_b
        self.Wn_s = Wn_s
        self.W_h = W_h
        self.W_1 = W_1
        self.W_2 = W_2
        self.W_3 = W_3
        self.b_t = b_t
        self.b_alpha = b_alpha
        self.bn_b = bn_b
        self.bn_s = bn_s
        self.b_h = b_h
        self.Vt_h = Vt_h
        self.Vt_v = Vt_v
        self.b_lambda = b_lambda
        self.Vh_alpha = Vh_alpha
        self.Vv_alpha = Vv_alpha
        self.Va_b = Va_b
        self.Va_s = Va_s

        self.tau_i = np.zeros((HIDDEN_LAYER_DIM, 1))
        self.b_i = np.zeros((HIDDEN_LAYER_DIM, 1))
        self.eta_i = np.zeros((HIDDEN_LAYER_DIM, 1))
        self.h_i = np.zeros((HIDDEN_LAYER_DIM, 1))
        self.u_theta_t = 0
        self.last_time = 0.0
        self.curr_time = None
        self.Q = 1.0
        self.c1 = 1.0
        self.u = self.RS.uniform()
        self.last_price = None
        self.curr_price = None
        self.loglikelihood = 0
        self.amount_before_trade = None
        print("using RL strategy")

    def get_next_action_time(self, event):
        # if no trade event occurred before, then amount before trade is same as max amount
        if self.amount_before_trade is None:
            self.amount_before_trade = MAX_AMT

        # if this method is called after buying/selling action, then sample new u
        if self.curr_price is None:
            # This is the first event
            self.curr_price = event.v_curr
            self.last_price = event.v_curr

        # prev_q = self.Q
        if event.is_trade_feedback or self.curr_time is None:
            self.u = self.RS.uniform()
            self.Q = 1
        else:
            self.Q *= (1 - self.cdf(event.t_i))

        # sample t_i
        v_delta = (self.curr_price - self.last_price)
        vth_val = np.array(self.Vt_h).dot(self.h_i)
        vtv_val = (self.Vt_v.dot(v_delta))
        bias = self.b_lambda
        self.c1 = np.exp(vth_val +
                         vtv_val +
                         bias)
        D = 1 - (self.wt / self.c1) * np.log((1 - self.u) / self.Q)
        a = np.squeeze((1 - self.u) / self.Q)
        assert a < 1
        assert np.log(D) > 0

        self.last_time = event.t_i
        new_t_i = self.last_time + (1 / self.wt) * np.log(D)
        new_t_i = np.asarray(new_t_i).squeeze()
        self.curr_time = new_t_i
        assert self.curr_time >= self.last_time

        return self.curr_time

    def calculate_tickfb_LL_int(self, event):
        # calculate log likelihood i.e. prob of no event happening between last event and this read event
        t_0 = 0.0
        u_theta_0 = self.c1 * np.exp(t_0)
        self.u_theta_t = self.c1 * np.squeeze(np.exp(self.wt * (event.t_i - self.last_time)))
        LL_int_numpy = np.squeeze((self.u_theta_t - u_theta_0) / self.wt)
        self.loglikelihood -= LL_int_numpy

    def get_next_action_item(self, event):
        self.last_price = self.curr_price
        self.curr_price = event.v_curr

        self.amount_before_trade = self.current_amt

        # sample alpha_i
        prob = 1 / (1 + np.exp(
            -np.array(self.Vh_alpha).dot(self.h_i) - np.array(self.Vv_alpha).dot((self.curr_price - self.last_price))))
        prob = np.squeeze(prob)
        prob_alpha = np.array([prob, 1.0 - prob])
        alpha_i = self.RS.choice(np.array([0, 1]), p=prob_alpha)

        # return empty trade details when the balance is insufficient to make a trade
        if self.current_amt <= (BASE_CHARGES + event.v_curr * PERCENTAGE_CHARGES):
            n_i = 0
            return alpha_i, n_i

        # subtract the fixed transaction charges
        self.current_amt -= BASE_CHARGES
        assert self.current_amt > 0
        if alpha_i == 0:
            A = np.array(self.Va_b).dot(self.h_i)
            # if np.all([np.equal(ele, 0.0) for ele in A]):
            #     A[0] = 1.0
            # A = np.append(np.array([[1]]), A, axis=0)

            # calculate mask
            max_share_buy = max(1, min(MAX_SHARE, int(np.floor(self.current_amt /
                                                               (event.v_curr + (
                                                                       event.v_curr * PERCENTAGE_CHARGES))))))
            mask = np.expand_dims(np.append(np.ones(max_share_buy),
                                            np.zeros(MAX_SHARE - max_share_buy)), axis=1)

            # apply mask
            # masked_A = np.multiply(mask, A)
            # masked_A[:max_share_buy] = np.exp(masked_A[:max_share_buy])
            # prob_n = masked_A / np.sum(masked_A[:max_share_buy])
            exp_A = np.exp(A)
            masked_A = np.multiply(mask, exp_A)

            reduce_sum = np.sum(masked_A)
            # check if the sum is zero and assign epsilon to avoid divide by zero and NaN value
            if np.abs(reduce_sum) < EPSILON:
                reduce_sum = EPSILON
            prob_n = masked_A / reduce_sum
            prob_n = np.squeeze(prob_n)
            # print("exp_A: ", exp_A)
            # print("max_share_buy: ", max_share_buy)
            # print("reduce_sum: ", reduce_sum)
            # print("prob_n: ", prob_n)
            # print("current_amt: ", self.current_amt)
            # print("v_curr: ", event.v_curr)
            # sample
            n_i = self.RS.choice(np.arange(MAX_SHARE), p=np.squeeze(prob_n))
            # print("n_i: ", n_i)
            # print("val: ", np.log(prob_n[n_i]))

        else:
            A = np.array(self.Va_s).dot(self.h_i)
            # if np.all([np.equal(ele, 0.0) for ele in A]):
            #     A[0] = 1.0
            # A[0] = 1.0
            # A = np.append(np.array([[1]]), A, axis=0)
            num_share_sell = int((self.owned_shares * event.v_curr) /
                                 (event.v_curr + (event.v_curr * PERCENTAGE_CHARGES)))
            max_share_sell = max(1, min(MAX_SHARE, num_share_sell))
            mask = np.expand_dims(np.append(np.ones(max_share_sell),
                                            np.zeros(MAX_SHARE - max_share_sell)), axis=1)
            # apply mask
            # masked_A = np.multiply(mask, A)
            # masked_A[:max_share_sell] = np.exp(masked_A[:max_share_sell])
            # prob_n = masked_A / np.sum(masked_A[:max_share_sell])
            exp_A = np.exp(A)
            masked_A = np.multiply(mask, exp_A)
            # prob_n = masked_A / np.sum(masked_A)
            reduce_sum = np.sum(masked_A)
            # check if the sum is zero and assign epsilon to avoid divide by zero and NaN value
            if np.abs(reduce_sum) < EPSILON:
                reduce_sum = EPSILON
            prob_n = masked_A / reduce_sum
            prob_n = np.squeeze(prob_n)
            # print("exp_A: ", exp_A)
            # print("max_share_sell: ", max_share_sell)
            # print("reduce_sum: ", reduce_sum)
            # print("prob_n: ", prob_n)
            # sample
            n_i = self.RS.choice(np.arange(MAX_SHARE), p=np.squeeze(prob_n))
            # print("n_i: ", n_i)
            # print("val: ", np.log(prob_n[n_i]))

        # encode event details
        t_delta = (self.curr_time - self.last_time)
        v_delta = (self.curr_price - self.last_price)
        # print("t_delta: ", t_delta)
        # print("v_delta: ", v_delta)
        self.tau_i = np.array(self.W_t).dot(t_delta) + self.b_t
        self.b_i = np.array(self.Wb_alpha).dot(1 - alpha_i) + np.array(self.Ws_alpha).dot(alpha_i) + self.b_alpha
        if alpha_i == 0:
            self.eta_i = np.array(self.Wn_b).dot(n_i) + self.bn_b
        else:
            self.eta_i = np.array(self.Wn_s).dot(n_i) + self.bn_s

        # update current amt
        if alpha_i == 0:
            self.current_amt -= n_i * event.v_curr
        elif alpha_i == 1:
            self.current_amt += n_i * event.v_curr

        # if n_i=0 i.e. there was no trade, add the base charges, which was previously deducted
        if n_i == 0:
            self.current_amt += BASE_CHARGES
        # subtract the percentage transaction charges
        a = event.v_curr * n_i * PERCENTAGE_CHARGES
        assert self.current_amt > a
        self.current_amt -= a
        assert self.current_amt > 0

        # update h_i i.e. h_next
        self.h_i = np.tanh(np.array(self.W_h).dot(self.h_i) + np.array(self.W_1).dot(self.tau_i)
                           + np.array(self.W_2).dot(self.b_i) + np.array(self.W_3).dot(self.eta_i) + self.b_h)
        # update log likelihood
        self.u_theta_t = self.c1 * np.squeeze(np.exp(self.wt * (self.curr_time - self.last_time)))

        # calculate log likelihood i.e. prob of no event happening between last event and this trade event
        t_0 = 0.0
        u_theta_0 = self.c1 * np.exp(t_0)
        LL_int_numpy = np.squeeze((self.u_theta_t - u_theta_0) / self.wt)
        self.loglikelihood -= LL_int_numpy

        # Log likelihood of happening this event
        LL_log_numpy = np.squeeze(np.log(self.u_theta_t))
        LL_alpha_i_numpy = np.log(prob_alpha[alpha_i])
        LL_n_i_numpy = np.log(prob_n[n_i])
        # print("LL_log_numpy: ",LL_log_numpy)
        # print("LL_alpha_i-numpy: ", LL_alpha_i_numpy)
        # print("LL_n_i_numpy: ", LL_n_i_numpy)
        # print()

        self.loglikelihood += LL_log_numpy + LL_alpha_i_numpy + LL_n_i_numpy

        return alpha_i, n_i

    def cdf(self, t):
        """Calculates the CDF assuming that the last event was at self.t_last"""
        # if self.wt == 0:
        #     return 1 - np.exp(- self.c1 * (t - self.last_time))
        # else:
        return 1 - np.exp((self.c1 / self.wt) * (1 - np.exp(self.wt * (t - self.last_time))))

    def get_LL_last_interval(self, end_time):
        # add LL for last interval
        self.c1 = np.exp(
            np.array(self.Vt_h).dot(self.h_i) + (self.Vt_v * (self.curr_price - self.last_price)) + self.b_lambda)
        u_theta_0 = self.c1 * np.exp(0.0)
        self.u_theta_t = self.c1 * np.squeeze(np.exp(self.wt * (end_time - self.last_time)))
        LL_last_term_numpy = np.squeeze((self.u_theta_t - u_theta_0) / self.wt)
        # print("t_delta: ",(end_time - self.last_time))
        # print("LL_last_term_numpy: ", LL_last_term_numpy)
        return LL_last_term_numpy

    def get_LL(self):
        return self.loglikelihood


class Environment:
    def __init__(self, T, time_gap, raw_data, agent, start_time, RS):
        self.T = T
        # self.state = State(curr_time=start_time)
        self.list_t_delta = []  # list of time delta
        self.list_alpha_i = []  # list of alpha_i's
        self.list_n_i = []  # list of n_i's
        self.list_v_curr = []  # list of current share price
        self.list_is_trade_feedback = []  # list of type of feedback: it is true if current event is trade
        self.list_current_amount = []  # list of amount available after current trade
        self.list_portfolio = []  # list of number of shares in possession
        self.list_v_delta = []  # store the difference between price of shares at last and current trade event (not read event)
        self.v_last = 0  # save final value of share, needed to calculate reward

        self.time_gap = time_gap
        self.raw_data = raw_data
        self.agent = agent
        self.curr_time = start_time
        self.RS = RS  # np.random.RandomState(seed)
        # for reading market value per minute
        if self.time_gap == "minute":
            # TODO need to find a way to group by minute using unix timestamp
            self.tick_data = self.raw_data.groupby(self.raw_data["datetime"], as_index=False).last()
        elif self.time_gap == "second":
            self.tick_data = self.raw_data.groupby(self.raw_data["datetime"], as_index=False).last()

            # print(self.tick_data.head())
        else:
            raise ValueError("Time gap value '{}' not understood.".format(self.time_gap))

    # def get_state(self):
    #     return self.state
    def apply_event(self, event):
        self.list_t_delta.append(event.t_i - self.agent.last_time)
        self.list_alpha_i.append(event.alpha_i)
        self.list_n_i.append(event.n_i)
        self.list_v_curr.append([event.v_curr])
        self.list_is_trade_feedback.append(event.is_trade_feedback)
        self.list_current_amount.append([event.event_curr_amt])
        self.list_portfolio.append([event.portfolio])
        self.list_v_delta.append([event.v_curr - self.agent.last_price])
        # print()

    def simulator(self):
        row_iterator = self.tick_data.iterrows()
        first_tick = next(row_iterator)[1]
        current_event = TickFeedback(t_i=first_tick.datetime, v_curr=first_tick.price,
                                     event_curr_amt=self.agent.amount_before_trade, portfolio=self.agent.owned_shares)
        # first read event is not saved
        # self.apply_event(current_event)
        print("trading..")

        for (_idx, next_tick) in row_iterator:
            while self.curr_time <= self.T:
                next_agent_action_time = self.agent.get_next_action_time(current_event)
                # check if there is enough amount to buy at least one share at current price
                if next_agent_action_time > next_tick.datetime:
                    current_event = TickFeedback(t_i=next_tick.datetime, v_curr=next_tick.price,
                                                 event_curr_amt=self.agent.amount_before_trade,
                                                 portfolio=self.agent.owned_shares)
                    self.agent.calculate_tickfb_LL_int(current_event)
                    # print("reading market value at time {}".format(current_event.t_i))
                    # break
                    # update the current time to read time
                    self.curr_time = next_tick.datetime
                    self.apply_event(current_event)
                    self.v_last = current_event.v_curr
                    break
                else:
                    # TODO update price: interpolate
                    trade_price = current_event.v_curr
                    alpha_i, n_i = self.agent.get_next_action_item(current_event)
                    current_event = TradeFeedback(t_i=next_agent_action_time, v_curr=trade_price,
                                                  alpha_i=alpha_i, n_i=n_i,
                                                  event_curr_amt=self.agent.amount_before_trade,
                                                  portfolio=self.agent.owned_shares)
                    self.agent.update_owned_shares(current_event)
                    # update the current time to trade time
                    self.curr_time = next_agent_action_time
                    self.apply_event(current_event)
                    self.v_last = current_event.v_curr

        self.agent.loglikelihood += self.agent.get_LL_last_interval(end_time=self.T)
        print("LL:", self.agent.get_LL())

    def get_last_interval(self):
        return self.T - self.agent.last_time

    def reward_fn(self):
        reward = MAX_AMT
        owned_shares = 0
        print("calculating reward...")
        for idx in range(len(self.list_t_delta)):
            if self.list_alpha_i[idx] == 0:
                reward -= self.list_n_i[idx] * self.list_v_curr[idx][0]
                owned_shares += self.list_n_i[idx]
            elif self.list_alpha_i[idx] == 1:
                reward += self.list_n_i[idx] * self.list_v_curr[idx][0]
                owned_shares -= self.list_n_i[idx]
            if self.list_n_i[idx] != 0:
                reward -= BASE_CHARGES
            reward -= (self.list_n_i[idx] * self.list_v_curr[idx][0] * PERCENTAGE_CHARGES)
        reward += owned_shares * self.v_last
        print("reward:{}".format(reward))
        return reward

    def get_num_events(self):
        return len(self.list_t_delta)


class ExpRecurrentTrader:
    def __init__(self, wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s,
                 W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h,
                 Vt_h, Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s,
                 num_hidden_states, sess, scope, batch_size, learning_rate, clip_norm,
                 summary_dir, save_dir, decay_steps, decay_rate, momentum,
                 device_cpu, device_gpu, only_cpu, max_events, q):
        self.summary_dir = summary_dir
        self.save_dir = save_dir
        self.tf_dtype = tf.float32
        self.np_dtype = np.float32

        self.learning_rate = learning_rate
        self.decay_rate = decay_rate
        self.decay_steps = decay_steps
        self.clip_norm = clip_norm

        self.q = q

        self.batch_size = batch_size

        self.tf_batch_size = None

        self.tf_max_events = None
        self.num_hidden_states = num_hidden_states
        self.types_of_portfolio = 1

        self.scope = scope or type(self).__name__
        var_device = device_cpu if only_cpu else device_gpu
        with tf.device(device_cpu):
            # Global step needs to be on the CPU
            self.global_step = tf.Variable(0, name='global_step', trainable=False)

            with tf.variable_scope(self.scope):
                with tf.variable_scope('hidden_state'):
                    with tf.device(var_device):
                        self.tf_W_t = tf.get_variable(name='W_t', shape=W_t.shape,
                                                      initializer=tf.constant_initializer(W_t), dtype=self.tf_dtype)
                        self.tf_Wb_alpha = tf.get_variable(name='Wb_alpha', shape=Wb_alpha.shape,
                                                           initializer=tf.constant_initializer(Wb_alpha),
                                                           dtype=self.tf_dtype)
                        self.tf_Ws_alpha = tf.get_variable(name='Ws_alpha', shape=Ws_alpha.shape,
                                                           initializer=tf.constant_initializer(Ws_alpha),
                                                           dtype=self.tf_dtype)
                        self.tf_Wn_b = tf.get_variable(name='Wn_b', shape=Wn_b.shape,
                                                       initializer=tf.constant_initializer(Wn_b), dtype=self.tf_dtype)
                        self.tf_Wn_s = tf.get_variable(name='Wn_s', shape=Wn_s.shape,
                                                       initializer=tf.constant_initializer(Wn_s), dtype=self.tf_dtype)
                        self.tf_W_h = tf.get_variable(name='W_h', shape=W_h.shape,
                                                      initializer=tf.constant_initializer(W_h), dtype=self.tf_dtype)
                        self.tf_W_1 = tf.get_variable(name='W_1', shape=W_1.shape,
                                                      initializer=tf.constant_initializer(W_1), dtype=self.tf_dtype)
                        self.tf_W_2 = tf.get_variable(name='W_2', shape=W_2.shape,
                                                      initializer=tf.constant_initializer(W_2), dtype=self.tf_dtype)
                        self.tf_W_3 = tf.get_variable(name='W_3', shape=W_3.shape,
                                                      initializer=tf.constant_initializer(W_3), dtype=self.tf_dtype)
                        self.tf_b_t = tf.get_variable(name='b_t', shape=b_t.shape,
                                                      initializer=tf.constant_initializer(b_t), dtype=self.tf_dtype)
                        self.tf_b_alpha = tf.get_variable(name='b_alpha', shape=b_alpha.shape,
                                                          initializer=tf.constant_initializer(b_alpha),
                                                          dtype=self.tf_dtype)
                        self.tf_bn_b = tf.get_variable(name='bn_b', shape=bn_b.shape,
                                                       initializer=tf.constant_initializer(bn_b), dtype=self.tf_dtype)
                        self.tf_bn_s = tf.get_variable(name='bn_s', shape=bn_s.shape,
                                                       initializer=tf.constant_initializer(bn_s), dtype=self.tf_dtype)
                        self.tf_b_h = tf.get_variable(name='b_h', shape=b_h.shape,
                                                      initializer=tf.constant_initializer(b_h), dtype=self.tf_dtype)

                        # Needed to calculate the hidden state for one step.
                        self.tf_h_i = tf.get_variable(name='h_i', initializer=tf.zeros((self.num_hidden_states, 1),
                                                                                       dtype=self.tf_dtype))
                        self.tf_tau_i = tf.get_variable(name='tau_i', initializer=tf.zeros((self.num_hidden_states, 1),
                                                                                           dtype=self.tf_dtype))
                        self.tf_b_i = tf.get_variable(name='b_i', initializer=tf.zeros((self.num_hidden_states, 1),
                                                                                       dtype=self.tf_dtype))
                        self.tf_eta_i = tf.get_variable(name='eta_i', initializer=tf.zeros((self.num_hidden_states, 1),
                                                                                           dtype=self.tf_dtype))
                        # self.tf_h_next = tf.nn.tanh(
                        #     tf.einsum('aij,ai->aj', self.tf_W_h, self.tf_h_i) +
                        #     tf.einsum('aij,ai->aj', self.tf_W_1, tau_i) +
                        #     tf.einsum('aij,ai->aj', self.tf_W_2, b_i) +
                        #     tf.einsum('aij,ai->aj', self.tf_W_3, eta_i) +
                        #     tf.squeeze(self.tf_b_h, axis=-1),
                        #     name='h_next'
                        # )

                with tf.variable_scope('output'):
                    with tf.device(var_device):
                        self.tf_wt = tf.get_variable(name='wt', shape=wt.shape,
                                                     initializer=tf.constant_initializer(wt), dtype=self.tf_dtype)
                        self.tf_Vt_h = tf.get_variable(name='Vt_h', shape=Vt_h.shape,
                                                       initializer=tf.constant_initializer(Vt_h), dtype=self.tf_dtype)
                        self.tf_Vt_v = tf.get_variable(name='Vt_v', shape=Vt_v.shape,
                                                       initializer=tf.constant_initializer(Vt_v), dtype=self.tf_dtype)
                        self.tf_b_lambda = tf.get_variable(name='b_lambda', shape=b_lambda.shape,
                                                           initializer=tf.constant_initializer(b_lambda),
                                                           dtype=self.tf_dtype)
                        self.tf_Vh_alpha = tf.get_variable(name='Vh_alpha', shape=Vh_alpha.shape,
                                                           initializer=tf.constant_initializer(Vh_alpha),
                                                           dtype=self.tf_dtype)
                        self.tf_Vv_alpha = tf.get_variable(name='Vv_alpha', shape=Vv_alpha.shape,
                                                           initializer=tf.constant_initializer(Vv_alpha),
                                                           dtype=self.tf_dtype)
                        self.tf_Va_b = tf.get_variable(name='Va_b', shape=Va_b.shape,
                                                       initializer=tf.constant_initializer(Va_b), dtype=self.tf_dtype)
                        self.tf_Va_s = tf.get_variable(name='Va_s', shape=Va_s.shape,
                                                       initializer=tf.constant_initializer(Va_s), dtype=self.tf_dtype)

                # Create a large dynamic_rnn kind of network which can calculate
                # the gradients for a given batch of simulations.
                with tf.variable_scope('training'):
                    self.tf_batch_rewards = tf.placeholder(name='rewards',
                                                           shape=(self.tf_batch_size, 1),
                                                           dtype=self.tf_dtype)
                    self.tf_batch_t_deltas = tf.placeholder(name='t_deltas',
                                                            shape=(self.tf_batch_size, self.tf_max_events),
                                                            dtype=self.tf_dtype)
                    self.tf_batch_seq_len = tf.placeholder(name='seq_len',
                                                           shape=(self.tf_batch_size, 1),
                                                           dtype=self.tf_dtype)
                    self.tf_batch_last_interval = tf.placeholder(name='last_interval',
                                                                 shape=(self.tf_batch_size, 1),
                                                                 dtype=self.tf_dtype)
                    self.tf_batch_alpha_i = tf.placeholder(name='alpha_i',
                                                           shape=(self.tf_batch_size, self.tf_max_events),
                                                           dtype=tf.int32)
                    self.tf_batch_n_i = tf.placeholder(name='n_i',
                                                       shape=(self.tf_batch_size, self.tf_max_events),
                                                       dtype=tf.int32)
                    self.tf_batch_v_curr = tf.placeholder(name='v_curr',
                                                          shape=(self.tf_batch_size, self.tf_max_events,
                                                                 self.types_of_portfolio),
                                                          dtype=self.tf_dtype)
                    self.tf_batch_is_trade_feedback = tf.placeholder(name='is_trade_feedback',
                                                                     shape=(self.tf_batch_size, self.tf_max_events),
                                                                     dtype=self.tf_dtype)
                    self.tf_batch_current_amt = tf.placeholder(name='current_amt',
                                                               shape=(self.tf_batch_size, self.tf_max_events,
                                                                      self.types_of_portfolio),
                                                               dtype=self.tf_dtype)
                    self.tf_batch_portfolio = tf.placeholder(name='portfolio',
                                                             shape=(self.tf_batch_size, self.tf_max_events,
                                                                    self.types_of_portfolio),
                                                             dtype=self.tf_dtype)
                    self.tf_batch_v_deltas = tf.placeholder(name='v_deltas',
                                                            shape=(self.tf_batch_size, self.tf_max_events,
                                                                   self.types_of_portfolio),
                                                            dtype=self.tf_dtype)

                    # Inferred batch size
                    inf_batch_size = tf.shape(self.tf_batch_t_deltas)[0]

                    self.tf_batch_init_h = tf.zeros(
                        name='init_h',
                        shape=(inf_batch_size, self.num_hidden_states),
                        dtype=self.tf_dtype
                    )
                    # Stacked version (for performance)

                    with tf.name_scope('stacked'):
                        with tf.device(var_device):
                            (self.W_t_mini, self.Wb_alpha_mini, self.Ws_alpha_mini,
                             self.Wn_b_mini, self.Wn_s_mini, self.W_h_mini,
                             self.W_1_mini, self.W_2_mini, self.W_3_mini,
                             self.b_t_mini, self.b_alpha_mini, self.bn_b_mini,
                             self.bn_s_mini, self.b_h_mini, self.wt_mini,
                             self.Vt_h_mini, self.Vt_v_mini, self.b_lambda_mini,
                             self.Vh_alpha_mini, self.Vv_alpha_mini, self.Va_b_mini,
                             self.Va_s_mini) = [
                                tf.stack(x, name=name)
                                for x, name in zip(
                                    zip(*[
                                        (tf.identity(self.tf_W_t), tf.identity(self.tf_Wb_alpha),
                                         tf.identity(self.tf_Ws_alpha), tf.identity(self.tf_Wn_b),
                                         tf.identity(self.tf_Wn_s), tf.identity(self.tf_W_h),
                                         tf.identity(self.tf_W_1), tf.identity(self.tf_W_2),
                                         tf.identity(self.tf_W_3), tf.identity(self.tf_b_t),
                                         tf.identity(self.tf_b_alpha), tf.identity(self.tf_bn_b),
                                         tf.identity(self.tf_bn_s), tf.identity(self.tf_b_h),
                                         tf.identity(self.tf_wt), tf.identity(self.tf_Vt_h),
                                         tf.identity(self.tf_Vt_v), tf.identity(self.tf_b_lambda),
                                         tf.identity(self.tf_Vh_alpha), tf.identity(self.tf_Vv_alpha),
                                         tf.identity(self.tf_Va_b), tf.identity(self.tf_Va_s))
                                        for _ in range(self.batch_size)
                                    ]),
                                    ['W_t', 'Wb_alpha', 'Ws_alpha', 'Wn_b', 'Wn_s', 'W_h', 'W_1', 'W_2', 'W_3',
                                     'b_t', 'b_alpha', 'bn_b', 'bn_s', 'b_h', 'wt', 'Vt_h', 'Vt_v', 'b_lambda',
                                     'Vh_alpha', 'Vv_alpha', 'Va_b', 'Va_s']
                                )
                            ]

                            self.rnn_cell_stack = TPPRExpMarkedCellStacked_finance(
                                hidden_state_size=(None, self.num_hidden_states),
                                output_size=[self.num_hidden_states] + [1] * 5,
                                tf_dtype=self.tf_dtype,
                                W_t=self.W_t_mini, Wb_alpha=self.Wb_alpha_mini,
                                Ws_alpha=self.Ws_alpha_mini, Wn_b=self.Wn_b_mini,
                                Wn_s=self.Wn_s_mini, W_h=self.W_h_mini,
                                W_1=self.W_1_mini, W_2=self.W_2_mini,
                                W_3=self.W_3_mini, b_t=self.b_t_mini,
                                b_alpha=self.b_alpha_mini, bn_b=self.bn_b_mini,
                                bn_s=self.bn_s_mini, b_h=self.b_h_mini, wt=self.wt_mini,
                                Vt_h=self.Vt_h_mini, Vt_v=self.Vt_v_mini,
                                b_lambda=self.b_lambda_mini, Vh_alpha=self.Vh_alpha_mini,
                                Vv_alpha=self.Vv_alpha_mini, Va_b=self.Va_b_mini, Va_s=self.Va_s_mini
                            )

                            ((self.h_states_stack, LL_log_terms_stack, LL_int_terms_stack, LL_alpha_i_stack,
                              LL_n_i_stack, loss_terms_stack),
                             tf_batch_h_t_mini) = tf.nn.dynamic_rnn(
                                cell=self.rnn_cell_stack,
                                inputs=(tf.expand_dims(self.tf_batch_t_deltas, axis=-1, name="dynRNN_t_delta"),
                                        tf.expand_dims(self.tf_batch_alpha_i, axis=-1, name="dynRNN_alpha_i"),
                                        tf.expand_dims(self.tf_batch_n_i, axis=-1, name="dynRNN_n_i"),
                                        self.tf_batch_v_curr,
                                        tf.expand_dims(self.tf_batch_is_trade_feedback, axis=-1,
                                                       name="dynRNN_is_trade_feedback"),
                                        self.tf_batch_current_amt,
                                        self.tf_batch_portfolio,
                                        self.tf_batch_v_deltas),
                                sequence_length=tf.squeeze(self.tf_batch_seq_len, axis=-1),
                                dtype=self.tf_dtype,
                                initial_state=self.tf_batch_init_h
                            )

                            self.LL_log_terms_stack = tf.squeeze(LL_log_terms_stack, axis=-1)
                            self.LL_int_terms_stack = tf.squeeze(LL_int_terms_stack, axis=-1)
                            self.LL_alpha_i_stack = tf.squeeze(LL_alpha_i_stack, axis=-1)
                            self.LL_n_i_stack = tf.squeeze(LL_n_i_stack, axis=-1)
                            self.loss_terms_stack = tf.squeeze(loss_terms_stack, axis=-1)

                            # LL_last_term_stack = rnn_cell.last_LL(tf_batch_h_t_mini, self.tf_batch_last_interval)
                            # loss_last_term_stack = rnn_cell.last_loss(tf_batch_h_t_mini, self.tf_batch_last_interval)

                            self.LL_last_term_stack = self.rnn_cell_stack.last_LL(tf_batch_h_t_mini,
                                                                                  self.tf_batch_v_deltas[:, -1, :],
                                                                                  self.tf_batch_last_interval)
                            self.loss_last_term_stack = self.rnn_cell_stack.last_loss(tf_batch_h_t_mini,
                                                                                      self.tf_batch_v_deltas[:, -1, :],
                                                                                      self.tf_batch_last_interval)

                            # self.LL_stack = self.LL_last_term_stack
                            self.LL_stack = (tf.reduce_sum(self.LL_log_terms_stack, axis=1)
                                             - tf.reduce_sum(self.LL_int_terms_stack, axis=1)
                                             + tf.reduce_sum(self.LL_alpha_i_stack, axis=1)
                                             + tf.reduce_sum(self.LL_n_i_stack, axis=1)
                                             + self.LL_last_term_stack)

                            tf_seq_len = tf.squeeze(self.tf_batch_seq_len, axis=-1)
                            self.loss_stack = (self.q / 2) * (tf.reduce_sum(self.loss_terms_stack, axis=1) +
                                               self.loss_last_term_stack)

                # with tf.name_scope('calc_u'):
                #     with tf.device(var_device):
                #         # These are operations needed to calculate u(t) in post-processing.
                #         # These can be done entirely in numpy-space, but since we have a
                #         # version in tensorflow, they have been moved here to avoid
                #         # memory leaks.
                #         # Otherwise, new additions to the graph were made whenever the
                #         # function calc_u was called.
                #
                #         self.calc_u_h_states = tf.placeholder(
                #             name='calc_u_h_states',
                #             shape=(self.tf_batch_size, self.num_hidden_states),
                #             dtype=self.tf_dtype
                #         )
                #         self.calc_u_batch_size = tf.placeholder(
                #             name='calc_u_batch_size',
                #             shape=(None,),
                #             dtype=tf.int32
                #         )
                #
                #         # TODO: formulas ??
                #         self.calc_u_c_is_init = tf.matmul(self.tf_Vt_h, self.tf_batch_init_h) + self.tf_b_lambda
                #         self.calc_u_c_is_rest = tf.squeeze(
                #             tf.matmul(
                #                 self.calc_u_h_states,
                #                 tf.tile(
                #                     tf.expand_dims(self.tf_Vt_h, 0),
                #                     [self.calc_u_batch_size[0], 1, 1]
                #                 )
                #             ) + self.tf_b_lambda,
                #             axis=-1,
                #             name='calc_u_c_is_rest'
                #         )
                #
                #         self.calc_u_is_own_event = tf.equal(self.tf_batch_b_idxes, 0)

                self.all_tf_vars = [self.tf_W_t, self.tf_Wb_alpha, self.tf_Ws_alpha,
                                    self.tf_Wn_b, self.tf_Wn_s, self.tf_W_h,
                                    self.tf_W_1, self.tf_W_2, self.tf_W_3,
                                    self.tf_b_t, self.tf_b_alpha, self.tf_bn_b,
                                    self.tf_bn_s, self.tf_b_h, self.tf_wt,
                                    self.tf_Vt_h, self.tf_Vt_v, self.tf_b_lambda,
                                    self.tf_Vh_alpha, self.tf_Vv_alpha, self.tf_Va_b,
                                    self.tf_Va_s]

                self.all_mini_vars = [self.W_t_mini, self.Wb_alpha_mini, self.Ws_alpha_mini,
                                      self.Wn_b_mini, self.Wn_s_mini, self.W_h_mini,
                                      self.W_1_mini, self.W_2_mini, self.W_3_mini,
                                      self.b_t_mini, self.b_alpha_mini, self.bn_b_mini,
                                      self.bn_s_mini, self.b_h_mini, self.wt_mini,
                                      self.Vt_h_mini, self.Vt_v_mini, self.b_lambda_mini,
                                      self.Vh_alpha_mini, self.Vv_alpha_mini, self.Va_b_mini,
                                      self.Va_s_mini]

                with tf.name_scope('stack_grad'):
                    with tf.device(var_device):
                        self.LL_grad_stacked = {x: tf.gradients(self.LL_stack, x)
                                                for x in self.all_mini_vars}
                        grads = {x: tf.gradients(self.loss_stack, x) for x in self.all_mini_vars}
                        self.loss_grad_stacked = {x: grads[x]
                                                    if None not in grads[x]
                                                    else [tf.zeros_like(x)]
                                                  for x in self.all_mini_vars}

                        self.avg_gradient_stack = []
                        avg_baseline = 0.0
                        # Removing the average reward + loss is not optimal baseline,
                        # but still reduces variance significantly.
                        coef = tf.squeeze(self.tf_batch_rewards, axis=-1) + self.loss_stack - avg_baseline
                        for x, y in zip(self.all_mini_vars, self.all_tf_vars):
                            LL_grad = self.LL_grad_stacked[x][0]
                            loss_grad = self.loss_grad_stacked[x][0]
                            # if self.set_wt_zero and y == self.tf_wt:
                            #     self.avg_gradient_stack.append(([0.0], y))
                            #     continue
                            dim = len(LL_grad.get_shape())
                            if dim == 1:
                                self.avg_gradient_stack.append(
                                    (tf.reduce_mean(LL_grad * coef + loss_grad, axis=0), y)
                                )
                            elif dim == 2:
                                self.avg_gradient_stack.append(
                                    (
                                        tf.reduce_mean(
                                            LL_grad * tf.tile(tf.reshape(coef, (-1, 1)),
                                                              [1, tf.shape(LL_grad)[1]]) +
                                            loss_grad,
                                            axis=0
                                        ),
                                        y
                                    )
                                )
                            elif dim == 3:
                                self.avg_gradient_stack.append(
                                    (
                                        tf.reduce_mean(
                                            LL_grad * tf.tile(tf.reshape(coef, (-1, 1, 1)),
                                                              [1, tf.shape(LL_grad)[1], tf.shape(LL_grad)[2]]) +
                                            loss_grad,
                                            axis=0
                                        ),
                                        y
                                    )
                                )
                            # TODO: write else to show error for dim 4

                        self.clipped_avg_gradients_stack, self.grad_norm_stack = \
                            tf.clip_by_global_norm(
                                [grad for grad, _ in self.avg_gradient_stack],
                                clip_norm=self.clip_norm
                            )

                        self.clipped_avg_gradient_stack = list(zip(
                            self.clipped_avg_gradients_stack,
                            [var for _, var in self.avg_gradient_stack]
                        ))

                self.tf_learning_rate = tf.train.inverse_time_decay(
                    self.learning_rate,
                    global_step=self.global_step,
                    decay_steps=self.decay_steps,
                    decay_rate=self.decay_rate
                )

                self.opt = tf.train.AdamOptimizer(
                    learning_rate=self.tf_learning_rate,
                    beta1=momentum
                )
                self.sgd_stacked_op = self.opt.apply_gradients(
                    self.clipped_avg_gradient_stack,
                    global_step=self.global_step
                )

                self.sess = sess

                # There are other global variables as well, like the ones which the
                # ADAM optimizer uses.
                self.saver = tf.train.Saver(
                    tf.global_variables(),
                    keep_checkpoint_every_n_hours=0.25,
                    max_to_keep=1000
                )

                # with tf.device(device_cpu):
                #     tf.contrib.training.add_gradients_summaries(self.avg_gradient_stack)
                #
                #     for v in self.all_tf_vars:
                #         variable_summaries(v)
                #
                #     variable_summaries(self.tf_learning_rate, name='learning_rate')
                #     variable_summaries(self.loss_stack, name='loss_stack')
                #     variable_summaries(self.LL_stack, name='LL_stack')
                #     variable_summaries(self.loss_last_term_stack, name='loss_last_term_stack')
                #     variable_summaries(self.LL_last_term_stack, name='LL_last_term_stack')
                #     variable_summaries(self.h_states_stack, name='hidden_states_stack')
                #     variable_summaries(self.LL_log_terms_stack, name='LL_log_terms_stack')
                #     variable_summaries(self.LL_int_terms_stack, name='LL_int_terms_stack')
                #     variable_summaries(self.loss_terms_stack, name='loss_terms_stack')
                #     variable_summaries(tf.cast(self.tf_batch_seq_len, self.tf_dtype), name='batch_seq_len')
                #
                #     self.tf_merged_summaries = tf.summary.merge_all()

    def initialize(self, finalize=True):
        """Initialize the graph."""
        self.sess.run(tf.global_variables_initializer())
        if finalize:
            # No more nodes will be added to the graph beyond this point.
            # Recommended way to prevent memory leaks afterwards, esp. if the
            # session will be used in a multi-threaded manner.
            # https://stackoverflow.com/questions/38694111/
            self.sess.graph.finalize()


def read_raw_data(seed):
    """ read raw_data """
    print("reading raw data")
    total_daily_files = 12751
    RS = np.random.RandomState(seed=seed)
    file_num = RS.choice(a=total_daily_files) # draw sample of size 1 with uniform distribution
    raw = pd.read_csv(SAVE_DIR + "/daily_data/{}_day.csv".format(file_num))
    # raw = pd.read_csv(SAVE_DIR + "/daily_data/0_day.csv")
    # raw = pd.read_csv(SAVE_DIR + "/0_day.csv")
    df = pd.DataFrame(raw)
    print(df.iloc[0:1]["datetime"])
    return df  # TODO return T and start_time


def make_default_trader_opts(seed=42):
    """Make default option set."""
    start_time = 1254130200
    scope = None
    decay_steps = 100
    decay_rate = 0.001
    num_hidden_states = HIDDEN_LAYER_DIM
    learning_rate = 0.001
    clip_norm = 1.0
    q = 0.05
    RS = np.random.RandomState(seed)
    wt = RS.randn(1)
    # wt = np.ones([1,1])
    # W_t = np.zeros((num_hidden_states, 1))
    # Wb_alpha = np.zeros((num_hidden_states, 1))
    # Ws_alpha = np.zeros((num_hidden_states, 1))
    # Wn_b = np.zeros((num_hidden_states, 1))
    # Wn_s = np.zeros((num_hidden_states, 1))
    # W_h = np.zeros((num_hidden_states, num_hidden_states))
    # W_1 = np.zeros((num_hidden_states, num_hidden_states))
    # W_2 = np.zeros((num_hidden_states, num_hidden_states))
    # W_3 = np.zeros((num_hidden_states, num_hidden_states))
    # b_t = np.zeros((num_hidden_states, 1))
    # b_alpha = np.zeros((num_hidden_states, 1))
    # bn_b = np.zeros((num_hidden_states, 1))
    # bn_s = np.zeros((num_hidden_states, 1))
    # b_h = np.zeros((num_hidden_states, 1))
    # Vt_h = np.ones((1, num_hidden_states))
    # Vt_v = np.zeros((1, 1))
    # b_lambda = np.zeros((1, 1))
    # Vh_alpha = np.zeros((1, num_hidden_states))
    # Vv_alpha = np.zeros((1, 1))
    # Va_b = np.zeros((100, num_hidden_states))
    # Va_s = np.zeros((100, num_hidden_states))

    W_t = RS.randn(num_hidden_states, 1)
    Wb_alpha = RS.randn(num_hidden_states, 1)
    Ws_alpha = RS.randn(num_hidden_states, 1)
    Wn_b = RS.randn(num_hidden_states, 1)
    Wn_s = RS.randn(num_hidden_states, 1)
    W_h = RS.randn(num_hidden_states, num_hidden_states) * 0.1 + np.diag(
        np.ones(num_hidden_states))  # Careful initialization
    W_1 = RS.randn(num_hidden_states, num_hidden_states)
    W_2 = RS.randn(num_hidden_states, num_hidden_states)
    W_3 = RS.randn(num_hidden_states, num_hidden_states)
    b_h = RS.randn(num_hidden_states, 1)
    b_t = RS.randn(num_hidden_states, 1)
    b_alpha = RS.randn(num_hidden_states, 1)
    bn_b = RS.randn(num_hidden_states, 1)
    bn_s = RS.randn(num_hidden_states, 1)
    Vt_h = RS.randn(1, num_hidden_states)
    Vt_v = RS.randn(1, TYPES_OF_PORTFOLIOS)
    b_lambda = RS.randn(1)
    Vh_alpha = RS.randn(1, num_hidden_states)
    Vv_alpha = RS.randn(1)
    Va_b = RS.randn(MAX_SHARE, num_hidden_states)
    Va_s = RS.randn(MAX_SHARE, num_hidden_states)

    # The graph execution time depends on this parameter even though each
    # trajectory may contain much fewer events. So it is wise to set
    # it such that it is just above the total number of events likely
    # to be seen.
    momentum = 0.9
    max_events = 1
    batch_size = 5
    T = 1254130260  # 5 seconds=1254130205, 0_day end time=1254153600, 0_hour end time=1254133800

    device_cpu = '/cpu:0'
    device_gpu = '/gpu:0'
    only_cpu = False
    save_dir = SAVE_DIR + "/results_TF_RL/"
    # Expected: './tpprl.summary/train-{}/'.format(run)
    summary_dir = save_dir + "/summary_dir/"
    return wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s, W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h, Vt_h, \
           Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s, num_hidden_states, scope, batch_size, learning_rate, \
           clip_norm, summary_dir, save_dir, decay_steps, decay_rate, momentum, device_cpu, device_gpu, only_cpu, \
           max_events, T, start_time, q


def get_feed_dict(trader, mgr):
    """Produce a feed_dict for the given list of scenarios."""
    batch_size = len(mgr)
    max_events = max(m.get_num_events() for m in mgr)
    # TODO modify following when multiple portfolios are considered

    full_shape = (batch_size, max_events)
    portfolio_shape = (batch_size, max_events, TYPES_OF_PORTFOLIOS)
    batch_rewards = np.asarray([m.reward_fn() for m in mgr])[:, np.newaxis]

    batch_last_interval = np.reshape(np.asarray([m.get_last_interval() for m in mgr], dtype=float),
                                     newshape=(batch_size, 1))

    batch_seq_len = np.asarray([m.get_num_events() for m in mgr], dtype=float)[:, np.newaxis]

    batch_t_delta = np.zeros(shape=full_shape, dtype=float)
    batch_alpha_i = np.zeros(shape=full_shape, dtype=int)
    batch_n_i = np.zeros(shape=full_shape, dtype=float)
    batch_v_curr = np.zeros(shape=portfolio_shape, dtype=float)
    batch_is_trade_feedback = np.zeros(shape=full_shape, dtype=int)
    batch_current_amount = np.zeros(shape=portfolio_shape, dtype=float)
    batch_portfolio = np.zeros(shape=portfolio_shape, dtype=float)
    batch_init_h = np.zeros(shape=(batch_size, trader.num_hidden_states), dtype=float)
    batch_v_delta = np.zeros(shape=portfolio_shape, dtype=float)

    for idx, m in enumerate(mgr):
        batch_len = int(batch_seq_len[idx])

        batch_t_delta[idx, 0:batch_len] = m.list_t_delta
        batch_alpha_i[idx, 0:batch_len] = m.list_alpha_i
        batch_n_i[idx, 0:batch_len] = m.list_n_i
        batch_v_curr[idx, 0:batch_len] = m.list_v_curr
        batch_is_trade_feedback[idx, 0:batch_len] = m.list_is_trade_feedback
        batch_current_amount[idx, 0:batch_len] = m.list_current_amount
        batch_portfolio[idx, 0:batch_len] = m.list_portfolio
        batch_v_delta[idx, 0:batch_len] = m.list_v_delta

    return {
        trader.tf_batch_t_deltas: batch_t_delta,
        trader.tf_batch_alpha_i: batch_alpha_i,
        trader.tf_batch_n_i: batch_n_i,
        trader.tf_batch_v_curr: batch_v_curr,
        trader.tf_batch_is_trade_feedback: batch_is_trade_feedback,
        trader.tf_batch_current_amt: batch_current_amount,
        trader.tf_batch_portfolio: batch_portfolio,
        trader.tf_batch_v_deltas: batch_v_delta,

        trader.tf_batch_rewards: batch_rewards,
        trader.tf_batch_seq_len: batch_seq_len,
        trader.tf_batch_init_h: batch_init_h,
        trader.tf_batch_last_interval: batch_last_interval,
    }


def run_scenario(trader, seed, T, start_time):
    # use seed to select the trade data file
    RS = np.random.RandomState(seed=seed)
    raw_data = read_raw_data(seed=seed)
    wt = trader.sess.run(trader.tf_wt)
    W_t = trader.sess.run(trader.tf_W_t)
    Wb_alpha = trader.sess.run(trader.tf_Wb_alpha)
    Ws_alpha = trader.sess.run(trader.tf_Ws_alpha)
    Wn_b = trader.sess.run(trader.tf_Wn_b)
    Wn_s = trader.sess.run(trader.tf_Wn_s)
    W_h = trader.sess.run(trader.tf_W_h)
    W_1 = trader.sess.run(trader.tf_W_1)
    W_2 = trader.sess.run(trader.tf_W_2)
    W_3 = trader.sess.run(trader.tf_W_3)
    b_t = trader.sess.run(trader.tf_b_t)
    b_alpha = trader.sess.run(trader.tf_b_alpha)
    bn_b = trader.sess.run(trader.tf_bn_b)
    bn_s = trader.sess.run(trader.tf_bn_s)
    b_h = trader.sess.run(trader.tf_b_h)
    Vt_h = trader.sess.run(trader.tf_Vt_h)
    Vt_v = trader.sess.run(trader.tf_Vt_v)
    b_lambda = trader.sess.run(trader.tf_b_lambda)
    Vh_alpha = trader.sess.run(trader.tf_Vh_alpha)
    Vv_alpha = trader.sess.run(trader.tf_Vv_alpha)
    Va_b = trader.sess.run(trader.tf_Va_b)
    Va_s = trader.sess.run(trader.tf_Va_s)
    # initiate agent/broadcaster
    # agent = SimpleStrategy(time_between_trades_secs=5)
    # agent = BollingerBandStrategy(window=20, num_std=2)
    agent = RLStrategy(wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s, W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h, Vt_h,
                       Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s, RS)
    # start time is set to '2009-09-28 09:30:00' i.e. 9:30 am of 28sept2009: 1254130200
    # max time T is set to '2009-09-28 16:00:00' i.e. same day 4pm: 1254153600
    mgr = Environment(T=T, time_gap="second", raw_data=raw_data, agent=agent, start_time=start_time, RS=RS)
    mgr.simulator()
    return mgr


def test_run_scenario():
    seed = 42
    # TODO create trader object
    wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s, W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h, Vt_h, \
    Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s, num_hidden_states, scope, batch_size, learning_rate, \
    clip_norm, summary_dir, save_dir, decay_steps, decay_rate, momentum, device_cpu, device_gpu, only_cpu, \
    max_events, T, start_time, q = make_default_trader_opts(seed=seed)

    print("default trader initialized..")

    sess = tf.Session()
    print("session created")

    trader = ExpRecurrentTrader(wt=wt, W_t=W_t, Wb_alpha=Wb_alpha, Ws_alpha=Ws_alpha, Wn_b=Wn_b, Wn_s=Wn_s, W_h=W_h,
                                W_1=W_1, W_2=W_2, W_3=W_3, b_t=b_t, b_alpha=b_alpha, bn_b=bn_b, bn_s=bn_s,
                                b_h=b_h, Vt_h=Vt_h, Vt_v=Vt_v, b_lambda=b_lambda, Vh_alpha=Vh_alpha, Vv_alpha=Vv_alpha,
                                Va_b=Va_b, Va_s=Va_s, num_hidden_states=num_hidden_states, sess=sess, scope=scope,
                                batch_size=batch_size, learning_rate=learning_rate, clip_norm=clip_norm,
                                summary_dir=summary_dir,
                                save_dir=save_dir, decay_steps=decay_steps, decay_rate=decay_rate, momentum=momentum,
                                device_cpu=device_cpu, device_gpu=device_gpu, only_cpu=only_cpu, max_events=max_events, q=q)
    trader.initialize()
    print("trader created")

    # TODO call run_scenario
    mgr = run_scenario(trader=trader, seed=seed, T=T, start_time=start_time)
    print("manager/environment created")

    # TODO call get_feed_dict
    feed_dict = get_feed_dict(trader=trader, mgr=mgr)
    # rwd = list(feed_dict.keys())[8]
    # print(feed_dict[rwd])
    # print("TF hidden states = {}".format(trader.sess.run([trader.h_states_stack], feed_dict=feed_dict)))

    print("TF LL_log_term_stack = {}".format(trader.sess.run([trader.LL_log_terms_stack], feed_dict=feed_dict)))
    print("TF LL_int_term_stack = {}".format(trader.sess.run([trader.LL_int_terms_stack], feed_dict=feed_dict)))
    print("TF LL_last_term_stack = {}".format(trader.sess.run([trader.LL_last_term_stack], feed_dict=feed_dict)))
    print("TF LL_alpha_i_stack = {}".format(trader.sess.run([trader.LL_alpha_i_stack], feed_dict=feed_dict)))
    print("TF LL_n_i_stack = {}".format(trader.sess.run([trader.LL_n_i_stack], feed_dict=feed_dict)))
    print('NN LL = {}'.format(mgr.agent.get_LL()))
    print('TF LL = {}'.format(trader.sess.run([trader.LL_stack], feed_dict=feed_dict)))
    '''
    (tf.reduce_sum(self.LL_log_terms_stack, axis=1) - 
    tf.reduce_sum(self.LL_int_terms_stack, axis=1)) + 
    self.LL_last_term_stack + self.LL_alpha_i_stack + self.LL_n_i_stack
    '''
    # import json
    # with open(save_dir+"/tf_feed_dict.json","w") as outfile:
    #     json.dump(feed_dict, outfile)
    # print("feed_dict saved as json at location:{}".format(save_dir+"/tf_feed_dict.json"))


def get_batch_feed_dicts(trader, seeds, T, start_time):
    seeds = list(seeds)
    simulations = [run_scenario(trader=trader, seed=sd, T=T, start_time=start_time) for sd in seeds]
    batch_feed_dicts = get_feed_dict(trader=trader, mgr=simulations)
    return batch_feed_dicts, simulations


def batch_test_run_scenario():
    wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s, W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h, Vt_h, \
    Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s, num_hidden_states, scope, batch_size, learning_rate, \
    clip_norm, summary_dir, save_dir, decay_steps, decay_rate, momentum, device_cpu, device_gpu, only_cpu, \
    max_events, T, start_time, q = make_default_trader_opts()
    print("default trader initialized..")

    sess = tf.Session()
    print("session created")

    trader = ExpRecurrentTrader(wt=wt, W_t=W_t, Wb_alpha=Wb_alpha, Ws_alpha=Ws_alpha, Wn_b=Wn_b, Wn_s=Wn_s, W_h=W_h,
                                W_1=W_1, W_2=W_2, W_3=W_3, b_t=b_t, b_alpha=b_alpha, bn_b=bn_b, bn_s=bn_s,
                                b_h=b_h, Vt_h=Vt_h, Vt_v=Vt_v, b_lambda=b_lambda, Vh_alpha=Vh_alpha, Vv_alpha=Vv_alpha,
                                Va_b=Va_b, Va_s=Va_s, num_hidden_states=num_hidden_states, sess=sess, scope=scope,
                                batch_size=batch_size, learning_rate=learning_rate, clip_norm=clip_norm,
                                summary_dir=summary_dir,
                                save_dir=save_dir, decay_steps=decay_steps, decay_rate=decay_rate, momentum=momentum,
                                device_cpu=device_cpu, device_gpu=device_gpu, only_cpu=only_cpu, max_events=max_events, q=q)
    trader.initialize()
    print("trader created")

    init_seed = 1337
    batches = 2
    batch_feed_dict, simulations = get_batch_feed_dicts(trader=trader, seeds=range(init_seed, init_seed + batches),
                                                        T=T, start_time=start_time)

    for sim in simulations:
        print('NN LL = {}'.format(sim.agent.get_LL()))

    print('TF LL = {}'.format(trader.sess.run([trader.LL_stack], feed_dict=batch_feed_dict)))
    # print('TF LL_log = {}'.format(trader.sess.run([trader.LL_log_terms_stack], feed_dict=batch_feed_dict)))
    # print('TF LL_alpha_i = {}'.format(trader.sess.run([trader.LL_alpha_i_stack], feed_dict=batch_feed_dict)))
    # print('TF LL_n_i = {}'.format(trader.sess.run([trader.LL_n_i_stack], feed_dict=batch_feed_dict)))


# backprop code
def create_environment_object(trader, seed, T, start_time):
    RS = np.random.RandomState(seed=seed)
    raw_data = read_raw_data(seed=seed)
    wt = trader.sess.run(trader.tf_wt)
    W_t = trader.sess.run(trader.tf_W_t)
    Wb_alpha = trader.sess.run(trader.tf_Wb_alpha)
    Ws_alpha = trader.sess.run(trader.tf_Ws_alpha)
    Wn_b = trader.sess.run(trader.tf_Wn_b)
    Wn_s = trader.sess.run(trader.tf_Wn_s)
    W_h = trader.sess.run(trader.tf_W_h)
    W_1 = trader.sess.run(trader.tf_W_1)
    W_2 = trader.sess.run(trader.tf_W_2)
    W_3 = trader.sess.run(trader.tf_W_3)
    b_t = trader.sess.run(trader.tf_b_t)
    b_alpha = trader.sess.run(trader.tf_b_alpha)
    bn_b = trader.sess.run(trader.tf_bn_b)
    bn_s = trader.sess.run(trader.tf_bn_s)
    b_h = trader.sess.run(trader.tf_b_h)
    Vt_h = trader.sess.run(trader.tf_Vt_h)
    Vt_v = trader.sess.run(trader.tf_Vt_v)
    b_lambda = trader.sess.run(trader.tf_b_lambda)
    Vh_alpha = trader.sess.run(trader.tf_Vh_alpha)
    Vv_alpha = trader.sess.run(trader.tf_Vv_alpha)
    Va_b = trader.sess.run(trader.tf_Va_b)
    Va_s = trader.sess.run(trader.tf_Va_s)
    # initiate agent/broadcaster
    # agent = SimpleStrategy(time_between_trades_secs=5)
    # agent = BollingerBandStrategy(window=20, num_std=2)
    agent = RLStrategy(wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s, W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h, Vt_h,
                       Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s, RS)
    # start time is set to '2009-09-28 09:30:00' i.e. 9:30 am of 28sept2009: 1254130200
    # max time T is set to '2009-09-28 16:00:00' i.e. same day 4pm: 1254153600
    mgr = Environment(T=T, time_gap="second", raw_data=raw_data, agent=agent, start_time=start_time, RS=RS)
    return mgr


def _simulation_worker(mgr):
    mgr.simulator()
    return mgr


def train_many(trader, num_iter, T, start_time, init_seed=42, with_MP=False):
    seed_start = init_seed + trader.sess.run(trader.global_step) * trader.batch_size
    train_op = trader.sgd_stacked_op
    grad_norm_op = trader.grad_norm_stack
    LL_op = trader.LL_stack
    loss_op = trader.loss_stack
    try:
        if with_MP:
            pool = MP.Pool()
        for iter_idx in range(num_iter):
            seed_end = seed_start + trader.batch_size
            seeds = range(seed_start, seed_end)
            if with_MP:
                raw_mgr = [create_environment_object(trader=trader, seed=sd, T=T, start_time=start_time) for sd in seeds]
                simulations = pool.map(_simulation_worker, raw_mgr)
            else:
                simulations = [run_scenario(trader=trader, seed=sd, T=T, start_time=start_time) for sd in seeds]
            num_events = [sim.get_num_events() for sim in simulations]
            f_d = get_feed_dict(trader=trader, mgr=simulations)
            reward, LL, loss, grad_norm, step, lr, _ = \
                trader.sess.run([trader.tf_batch_rewards, LL_op, loss_op,
                               grad_norm_op,
                               trader.global_step, trader.tf_learning_rate,
                               train_op],
                              feed_dict=f_d)

            mean_LL = np.mean(LL)
            std_LL = np.std(LL)

            mean_loss = np.mean(loss)
            std_loss = np.std(loss)

            mean_reward = np.mean(reward)
            std_reward = np.std(reward)

            mean_events = np.mean(num_events)
            std_events = np.std(num_events)

            print('* Run {}, LL {:.2f}±{:.2f}, loss {:.2f}±{:.2f}, Rwd {:.3f}±{:.3f}, events {:.2f}±{:.2f}'
                  ' seeds {}--{}, grad_norm {:.2f}, step = {}'
                  ', lr = {:.5f}, wt={:.5f}, b_lambda={:.5f}\n'
                  .format(iter_idx,
                          mean_LL, std_LL,
                          mean_loss, std_loss,
                          mean_reward, std_reward,
                          mean_events, std_events,
                          seed_start, seed_end - 1,
                          grad_norm, step, lr,
                          trader.sess.run(trader.tf_wt)[0], trader.sess.run(trader.tf_b_lambda)[0]))

            # Ready for the next iter_idx.
            seed_start = seed_end

            # if iter_idx % save_every == 0:
            #     print('Saving model!')
            #     self.saver.save(self.sess,
            #                     chkpt_file,
            #                     global_step=self.global_step, )
    finally:
        print("TODO: saving model")


def test_backprop_code():
    epochs = 5
    until = 50
    num_iter = 1  # number of trade data files

    wt, W_t, Wb_alpha, Ws_alpha, Wn_b, Wn_s, W_h, W_1, W_2, W_3, b_t, b_alpha, bn_b, bn_s, b_h, Vt_h, \
    Vt_v, b_lambda, Vh_alpha, Vv_alpha, Va_b, Va_s, num_hidden_states, scope, batch_size, learning_rate, \
    clip_norm, summary_dir, save_dir, decay_steps, decay_rate, momentum, device_cpu, device_gpu, only_cpu, \
    max_events, T, start_time, q = make_default_trader_opts()
    print("default trader initialized..")

    config = tf.ConfigProto(
        allow_soft_placement=True,
        log_device_placement=False
    )
    config.gpu_options.allow_growth = True

    sess = tf.Session(config=config)
    print("session created")

    trader = ExpRecurrentTrader(wt=wt, W_t=W_t, Wb_alpha=Wb_alpha, Ws_alpha=Ws_alpha, Wn_b=Wn_b, Wn_s=Wn_s, W_h=W_h,
                                W_1=W_1, W_2=W_2, W_3=W_3, b_t=b_t, b_alpha=b_alpha, bn_b=bn_b, bn_s=bn_s,
                                b_h=b_h, Vt_h=Vt_h, Vt_v=Vt_v, b_lambda=b_lambda, Vh_alpha=Vh_alpha, Vv_alpha=Vv_alpha,
                                Va_b=Va_b, Va_s=Va_s, num_hidden_states=num_hidden_states, sess=sess, scope=scope,
                                batch_size=batch_size, learning_rate=learning_rate, clip_norm=clip_norm,
                                summary_dir=summary_dir,
                                save_dir=save_dir, decay_steps=decay_steps, decay_rate=decay_rate, momentum=momentum,
                                device_cpu=device_cpu, device_gpu=device_gpu, only_cpu=only_cpu, max_events=max_events, q=q)

    trader.initialize()
    print("trader created")
    for epoch in range(epochs):
        print("\nEPOCH: {}".format(epoch))
        train_many(trader=trader, num_iter=num_iter, T=T, start_time=start_time, with_MP=True)
        step = trader.sess.run(trader.global_step)
        if step > until:
            print(
                'Have already run {} > {} iterations, not going further.'.format(step, until)
            )
            break


if __name__ == '__main__':
    # test_run_scenario()
    # batch_test_run_scenario()
    test_backprop_code()