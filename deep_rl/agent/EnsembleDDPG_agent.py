#######################################################################
# Copyright (C) 2017 Shangtong Zhang(zhangshangtong.cpp@gmail.com)    #
# Permission given to modify the code as long as you keep this        #
# declaration at the top                                              #
#######################################################################

from .BaseAgent import *

class EnsembleDDPGAgent(BaseAgent):
    def __init__(self, config):
        BaseAgent.__init__(self, config)
        self.config = config
        self.task = config.task_fn()
        self.network = config.network_fn(self.task.state_dim, self.task.action_dim)
        self.target_network = config.network_fn(self.task.state_dim, self.task.action_dim)
        self.target_network.load_state_dict(self.network.state_dict())
        self.replay = config.replay_fn()
        self.random_process = config.random_process_fn(self.task.action_dim)
        self.total_steps = 0

    def soft_update(self, target, src):
        for target_param, param in zip(target.parameters(), src.parameters()):
            target_param.detach_()
            target_param.copy_(target_param * (1.0 - self.config.target_network_mix) +
                                    param * self.config.target_network_mix)

    def evaluation_action(self, state):
        self.config.state_normalizer.set_read_only()
        state = np.stack([self.config.state_normalizer(state)])
        actions, q_values, _ = self.network.predict(state)
        greedy_option = torch.argmax(q_values, dim=-1)
        if self.config.option_type == 'per_step':
            actions = actions[0, greedy_option, :]
        elif self.config.option_type == 'per_episode':
            if not self.config.intro_q:
                actions = actions[0, greedy_option, :]
            else:
                if self.info['initial_state']:
                    self.info['episode_option'] = greedy_option.clone()
                actions = actions[0, self.info['episode_option'], :]
        else:
            raise Exception('Unknown option type')
        action = actions.cpu().detach().numpy().flatten()
        self.config.state_normalizer.unset_read_only()
        return action

    def episode(self, deterministic=False):
        self.random_process.reset_states()
        state = self.task.reset()
        state = self.config.state_normalizer(state)

        config = self.config
        steps = 0
        total_reward = 0.0
        while True:
            self.evaluate()
            self.evaluation_episodes()

            actions, q_values, _ = self.network.predict(np.stack([state]))

            random_option_prob = config.random_option_prob()
            if config.option_type == 'per_step' or (
                config.option_type == 'per_episode' and steps == 0
            ):
                if np.random.rand() < random_option_prob:
                    self.option = np.random.randint(config.num_options)
                else:
                    self.option = torch.argmax(q_values, dim=-1)
                    self.option = np.asscalar(self.option.detach().cpu().numpy())

            actions = actions.detach().cpu().numpy()
            action = actions[0, self.option, :].flatten()
            action += self.random_process.sample()

            next_state, reward, done, info = self.task.step(action)
            next_state = self.config.state_normalizer(next_state)
            total_reward += reward
            reward = self.config.reward_normalizer(reward)

            if not deterministic:
                mask = np.random.binomial(n=1, p=0.5, size=config.num_options)
                self.replay.feed([state, action, reward, next_state, int(done), self.option, mask])
                self.total_steps += 1

            steps += 1
            state = next_state

            if not deterministic and self.replay.size() >= config.min_memory_size:
                experiences = self.replay.sample()
                states, actions, rewards, next_states, terminals, options, masks = experiences
                masks = self.network.tensor(masks)
                phi_next = self.target_network.feature(next_states)
                a_next = self.target_network.actor(phi_next)
                q_next = self.target_network.critic(phi_next, a_next)
                if not config.infro_q:
                    q_next = q_next.max(1)[0].unsqueeze(1)
                terminals = self.network.tensor(terminals).unsqueeze(1)
                rewards = self.network.tensor(rewards).unsqueeze(1)
                q_next = config.discount * q_next * (1 - terminals)
                q_next.add_(rewards)
                q_next = q_next.detach()
                phi = self.network.feature(states)
                actions = self.network.tensor(actions)
                q = self.network.critic(phi, actions)
                if not config.off_policy_critic:
                    q = q[self.network.tensor(np.arange(q.size(0))).long(),
                          self.network.tensor(options).long()].unsqueeze(-1)
                q_loss = q - q_next
                if config.mask_q:
                    q_loss = q_loss * masks
                q_loss = q_loss.pow(2).mul(0.5).sum(1).mean()

                self.network.zero_grad()
                q_loss.backward()
                self.network.critic_opt.step()

                phi = self.network.feature(states)
                actions = self.network.actor(phi)
                q = self.network.critic(phi.detach(), actions)
                if not config.off_policy_actor:
                    q = q[self.network.tensor(np.arange(q.size(0))).long(),
                          self.network.tensor(options).long()].unsqueeze(-1)
                policy_loss = -q.sum(1).mean()
                self.network.zero_grad()
                policy_loss.backward()
                self.network.actor_opt.step()

                self.soft_update(self.target_network, self.network)

            if done:
                break

        return total_reward, steps
