import argparse
import numpy as np
import tensorflow as tf
import time
import pickle
import sys
sys.path.append('../')
import maddpg.common.tf_util as U
from maddpg.trainer.maddpg import MADDPGAgentTrainer
import tensorflow.contrib.layers as layers

exist_no_adversaries = False


def parse_args():
    parser = argparse.ArgumentParser("Reinforcement Learning experiments for multiagent environments")
    # Environment
    parser.add_argument("--scenario", type=str, default="simple_spread", help="name of the scenario script")
    parser.add_argument("--max-episode-len", type=int, default=30, help="maximum episode length")
    parser.add_argument("--num-episodes", type=int, default=60000, help="number of episodes")
    parser.add_argument("--num-adversaries", type=int, default=5, help="number of adversaries")
    parser.add_argument("--good-policy", type=str, default="maddpg", help="policy for good agents")
    parser.add_argument("--adv-policy", type=str, default="maddpg", help="policy of adversaries")
    # Core training parameters
    parser.add_argument("--lr", type=float, default=1e-2, help="learning rate for Adam optimizer")
    parser.add_argument("--gamma", type=float, default=0.95, help="discount factor")
    parser.add_argument("--batch-size", type=int, default=1024, help="number of episodes to optimize at the same time")
    parser.add_argument("--num-units", type=int, default=64, help="number of units in the mlp")
    # Checkpointing
    parser.add_argument("--exp-name", type=str, default=None, help="name of the experiment")
    parser.add_argument("--save-dir", type=str, default="./ckpt_simple_spread_5/test-model.ckpt", help="directory in which training state and model should be saved")
    parser.add_argument("--save-rate", type=int, default=1000, help="save model once every time this many episodes are completed")
    parser.add_argument("--load-dir", type=str, default="", help="directory in which training state and model are loaded")
    # Evaluation
    parser.add_argument("--restore", action="store_true", default=True)
    parser.add_argument("--display", action="store_true", default=False)
    parser.add_argument("--benchmark", action="store_true", default=False)
    parser.add_argument("--benchmark-iters", type=int, default=100000, help="number of iterations run for benchmarking")
    parser.add_argument("--benchmark-dir", type=str, default="./benchmark_files/", help="directory where benchmark data is saved")
    parser.add_argument("--plots-dir", type=str, default="./learning_curves/", help="directory where plot data is saved")
    return parser.parse_args()


def mlp_model(input, num_outputs, scope, reuse=False, num_units=64, rnn_cell=None):
    # This model takes as input an observation and returns values of all actions
    with tf.variable_scope(scope, reuse=reuse):
        out = input
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_outputs, activation_fn=None)
        return out


def mlp(input, scope, reuse=False, num_units=64):
    with tf.variable_scope(scope, reuse=reuse):
        out = input
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = tf.expand_dims(out, 1)
        return out


def multi_head_attention(v1, q1, k1, ve, scope, l=2, d=128, dv=16, d_out=64, nv=8, reuse=False):
    from keras import backend as K
    from keras.layers import Lambda
    with tf.variable_scope(scope, reuse=reuse):
        v2 = layers.fully_connected(v1, num_outputs=dv*nv, activation_fn=tf.nn.relu)
        q2 = layers.fully_connected(q1, num_outputs=dv*nv, activation_fn=tf.nn.relu)
        k2 = layers.fully_connected(k1, num_outputs=dv*nv, activation_fn=tf.nn.relu)
        v = tf.reshape(v2, (-1, l, nv, dv))
        q = tf.reshape(q2, (-1, l, nv, dv))
        k = tf.reshape(k2, (-1, l, nv, dv))
        v = tf.transpose(v, [0, 2, 1, 3])
        q = tf.transpose(q, [0, 2, 1, 3])
        k = tf.transpose(k, [0, 2, 1, 3])
        att = Lambda(lambda x: K.batch_dot(x[0], x[1], axes=[3, 3]) / np.sqrt(dv))([q, k])  # l, nv, nv
        att = Lambda(lambda x: K.softmax(x))(att)
        out = Lambda(lambda x: K.batch_dot(x[0], x[1], axes=[3, 2]))([att, v])
        out = Lambda(lambda x: K.permute_dimensions(x, (0, 2, 1, 3)))(out)
        out = tf.reshape(out, (-1, l, dv*nv))
        temp = tf.matmul(ve, out)

        out = layers.fully_connected(temp, num_outputs=d_out, activation_fn=tf.nn.relu)
        return out


def q_net(feature, relation1, num_outputs, scope, reuse=False):
    with tf.variable_scope(scope, reuse=reuse):
        h = tf.concat([feature, relation1], axis=2)
        h = tf.squeeze(h, axis=1)
        out = layers.fully_connected(h, num_outputs=num_outputs, activation_fn=tf.nn.relu)
        return out


def my_graph_model_policy_network(input1, input2, vec, neighbors, agent_n, num_outputs, scope, reuse=False, num_units=128):
    encoder = mlp
    # This model takes as input an observation and returns values of all actions
    with tf.variable_scope(scope, reuse=reuse):
        feature = []
        for _ in range(agent_n):
            feature.append(encoder(input1[_], scope='encoder', num_units=num_units, reuse=tf.AUTO_REUSE))
        feature_ = tf.concat(feature, axis=1)

        relation1 = []
        for _ in range(agent_n):
            temp = tf.matmul(input2[_], feature_)
            relation1.append(multi_head_attention(temp, temp, temp, vec, scope='multi_head_attention',
                                                  l=neighbors, reuse=tf.AUTO_REUSE))
        relation1_ = tf.concat(relation1, axis=1)

        '''relation2 = []
        for _ in range(agent_n):
            temp = tf.matmul(input2[_], relation1_)
            relation2.append(multi_head_attention(temp, temp, temp, vec, neighbors))'''

        out = []
        for _ in range(agent_n):
            out.append(q_net(feature[_], relation1[_], num_outputs, scope='q_net', reuse=tf.AUTO_REUSE))

        return out


def make_env(scenario_name, arglist, benchmark=False):
    from multiagent.environment import MultiAgentEnv
    import multiagent.scenarios as scenarios

    # load scenario from script
    scenario = scenarios.load(scenario_name + ".py").Scenario()
    # create world
    world = scenario.make_world()
    # create multiagent environment
    if benchmark:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, scenario.benchmark_data)
    else:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation)
    return env


def get_trainers(env, num_adversaries, obs_shape_n, arglist):
    trainers = []
    model = mlp_model
    trainer = MADDPGAgentTrainer
    trainers.append(trainer(
        "adversaries", my_graph_model_policy_network, model, obs_shape_n, env.action_space, num_adversaries,
        arglist, local_q_func=(arglist.adv_policy == 'ddpg')))
    if exist_no_adversaries:
        trainers.append(trainer(
            "no_adversaries", my_graph_model_policy_network, model, obs_shape_n, env.action_space, num_adversaries,
            arglist, local_q_func=(arglist.good_policy == 'ddpg')))
    return trainers


def train(arglist):
    with U.single_threaded_session():
        # Create environment
        env = make_env(arglist.scenario, arglist, arglist.benchmark)
        # Create agent trainers
        obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
        num_adversaries = min(env.n, arglist.num_adversaries)
        trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist)
        print('Using good policy {} and adv policy {}'.format(arglist.good_policy, arglist.adv_policy))

        # Initialize
        U.initialize()

        # Load previous results, if necessary
        if arglist.load_dir == "":
            arglist.load_dir = arglist.save_dir
        if arglist.display or arglist.restore or arglist.benchmark:
            print('Loading previous state...')
            U.load_state(arglist.load_dir)

        episode_rewards = [0.0]  # sum of rewards for all agents
        agent_rewards = [[0.0] for _ in range(env.n)]  # individual agent reward
        final_ep_rewards = []  # sum of rewards for training curve
        final_ep_ag_rewards = []  # agent rewards for training curve
        agent_info = [[[]]]  # placeholder for benchmarking info
        saver = tf.train.Saver()
        obs_n, adj_n = env.reset()
        episode_step = 0
        train_step = 0
        t_start = time.time()

        print('Starting iterations...')
        while True:
            # get action
            vec = np.array([[1, 0]])
            action_n1 = trainers[0].action(obs_n[0:num_adversaries] + adj_n[0:num_adversaries] + [vec])
            if exist_no_adversaries:
                vec = np.array([[1, 0]])
                action_n2 = trainers[1].action(obs_n[num_adversaries:env.n] + adj_n[num_adversaries:env.n] + [vec])
                action_n1.extend(action_n2)
            action_n = []
            for i, act in enumerate(action_n1):
                act = act.reshape((-1))
                action_n.append(act)

            # environment step
            new_obs_n, rew_n, done_n, info_n, new_adj_n = env.step(action_n)
            episode_step += 1
            done = all(done_n)
            terminal = (episode_step >= arglist.max_episode_len)

            # collect experience

            trainers[0].experience(obs_n[0:num_adversaries], action_n[0:num_adversaries],
                                   rew_n[0:num_adversaries], new_obs_n[0:num_adversaries],
                                   done_n[0:num_adversaries], adj_n[0:num_adversaries],
                                   new_adj_n[0:num_adversaries], terminal)
            if exist_no_adversaries:
                trainers[1].experience(obs_n[num_adversaries:env.n], action_n[num_adversaries:env.n],
                                       rew_n[num_adversaries:env.n], new_obs_n[num_adversaries:env.n],
                                       done_n[num_adversaries:env.n], adj_n[num_adversaries:env.n],
                                       new_adj_n[num_adversaries:env.n], terminal)

            # update observation
            obs_n = new_obs_n
            adj_n = new_adj_n

            for i, rew in enumerate(rew_n):
                episode_rewards[-1] += rew
                agent_rewards[i][-1] += rew

            if done or terminal:
                obs_n, adj_n = env.reset()
                episode_step = 0
                episode_rewards.append(0)
                for a in agent_rewards:
                    a.append(0)
                agent_info.append([[]])

            # increment global step counter
            train_step += 1

            # for benchmarking learned policies
            if arglist.benchmark:
                for i, info in enumerate(info_n):
                    agent_info[-1][i].append(info_n['n'])
                if train_step > arglist.benchmark_iters and (done or terminal):
                    file_name = arglist.benchmark_dir + arglist.exp_name + '.pkl'
                    print('Finished benchmarking, now saving...')
                    with open(file_name, 'wb') as fp:
                        pickle.dump(agent_info[:-1], fp)
                    break
                continue

            # for displaying learned policies
            if arglist.display:
                time.sleep(0.1)
                env.render()
                continue

            # update all trainers, if not in display or benchmark mode
            loss = None
            for agent in trainers:
                agent.pre_update()
            for agent in trainers:
                loss = agent.update(trainers, train_step)

            # save model, display training output
            if terminal and (len(episode_rewards) % arglist.save_rate == 0):
                U.save_state(arglist.save_dir, saver=saver)
                # print statement depends on whether or not there are adversaries
                if num_adversaries == 0:
                    print("steps: {}, episodes: {}, mean episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-arglist.save_rate:]), round(time.time()-t_start, 3)))
                else:
                    print("steps: {}, episodes: {}, mean episode reward: {}, agent episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-arglist.save_rate:]),
                        [np.mean(rew[-arglist.save_rate:]) for rew in agent_rewards], round(time.time()-t_start, 3)))
                t_start = time.time()
                # Keep track of final episode reward
                final_ep_rewards.append(np.mean(episode_rewards[-arglist.save_rate:]))
                for rew in agent_rewards:
                    final_ep_ag_rewards.append(np.mean(rew[-arglist.save_rate:]))

            # saves final episode reward for plotting training curve later
            if len(episode_rewards) > arglist.num_episodes:
                rew_file_name = arglist.plots_dir + arglist.exp_name + '_rewards.pkl'
                with open(rew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_rewards, fp)
                agrew_file_name = arglist.plots_dir + arglist.exp_name + '_agrewards.pkl'
                with open(agrew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_ag_rewards, fp)
                print('...Finished total of {} episodes.'.format(len(episode_rewards)))
                break

if __name__ == '__main__':
    arglist = parse_args()
    train(arglist)
