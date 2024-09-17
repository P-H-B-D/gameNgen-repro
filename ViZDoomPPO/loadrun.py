# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
#   /$$    /$$ /$$           /$$$$$$$                                          /$$$$$$$  /$$$$$$$   /$$$$$$    #
#  | $$   | $$|__/          | $$__  $$                                        | $$__  $$| $$__  $$ /$$__  $$   #
#  | $$   | $$ /$$ /$$$$$$$$| $$  \ $$  /$$$$$$   /$$$$$$  /$$$$$$/$$$$       | $$  \ $$| $$  \ $$| $$  \ $$   #
#  |  $$ / $$/| $$|____ /$$/| $$  | $$ /$$__  $$ /$$__  $$| $$_  $$_  $$      | $$$$$$$/| $$$$$$$/| $$  | $$   #
#   \  $$ $$/ | $$   /$$$$/ | $$  | $$| $$  \ $$| $$  \ $$| $$ \ $$ \ $$      | $$____/ | $$____/ | $$  | $$   #
#    \  $$$/  | $$  /$$__/  | $$  | $$| $$  | $$| $$  | $$| $$ | $$ | $$      | $$      | $$      | $$  | $$   #
#     \  $/   | $$ /$$$$$$$$| $$$$$$$/|  $$$$$$/|  $$$$$$/| $$ | $$ | $$      | $$      | $$      |  $$$$$$/   #
#      \_/    |__/|________/|_______/  \______/  \______/ |__/ |__/ |__/      |__/      |__/       \______/    #
#                                                                                                              #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #                                                                                                
                                                                                                         
# FORK OF LEANDRO KIELIGER'S DOOM PPO TUTORIAL: https://lkieliger.medium.com/deep-reinforcement-learning-in-practice-by-playing-doom-part-1-getting-started-618c99075c77                                                                                                       

# SCRIPT TO GENERATE A PARQUET OR GIF FROM PRETRAINED PPO AGENT. 

import imageio
import numpy as np
import vizdoom
from common import envs
import torch
import random
import string

from collections import deque
from vizdoom.vizdoom import GameVariable
from stable_baselines3.common.vec_env import (
    VecTransposeImage,
    DummyVecEnv,
    SubprocVecEnv,
)

from PIL import Image as pil_image

from datasets import Dataset, Features, Image, Value, Sequence


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# device=torch.device("mps")
print(f"Using device: {device}")

# Rewards
# 1 per kill
reward_factor_frag = 1.0
reward_factor_damage = 0.01

# Player can move at ~16.66 units per tick
reward_factor_distance = 5e-4
penalty_factor_distance = -2.5e-3
reward_threshold_distance = 3.0

# Pistol clips have 10 bullets
reward_factor_ammo_increment = 0.02
reward_factor_ammo_decrement = -0.01

# Player starts at 100 health
reward_factor_health_increment = 0.02
reward_factor_health_decrement = -0.01
reward_factor_armor_increment = 0.01

# List of game variables storing ammunition information. Used for keeping track of ammunition-related rewards.
AMMO_VARIABLES = [
    GameVariable.AMMO0,
    GameVariable.AMMO1,
    GameVariable.AMMO2,
    GameVariable.AMMO3,
    GameVariable.AMMO4,
    GameVariable.AMMO5,
    GameVariable.AMMO6,
    GameVariable.AMMO7,
    GameVariable.AMMO8,
    GameVariable.AMMO9,
]

# List of game variables storing weapon information. Used for keeping track of ammunition-related rewards.
WEAPON_VARIABLES = [
    GameVariable.WEAPON0,
    GameVariable.WEAPON1,
    GameVariable.WEAPON2,
    GameVariable.WEAPON3,
    GameVariable.WEAPON4,
    GameVariable.WEAPON5,
    GameVariable.WEAPON6,
    GameVariable.WEAPON7,
    GameVariable.WEAPON8,
    GameVariable.WEAPON9,
]


class DoomWithBotsShaped(envs.DoomWithBots):
    """An environment wrapper for a Doom deathmatch game with bots.

    Rewards are shaped according to the multipliers defined in the notebook.
    """

    def __init__(self, game, frame_processor, frame_skip, n_bots, shaping):
        super().__init__(game, frame_processor, frame_skip, n_bots)

        # Give a random two-letter name to the agent for identifying instances in parallel learning.
        self.name = "".join(random.choices(string.ascii_uppercase + string.digits, k=2))
        self.shaping = shaping

        # Internal states
        self.last_health = 100
        self.last_x, self.last_y = self._get_player_pos()
        self.ammo_state = self._get_ammo_state()
        self.weapon_state = self._get_weapon_state()
        self.total_rew = self.last_damage_dealt = self.deaths = self.last_frags = (
            self.last_armor
        ) = 0

        # Store individual reward contributions for logging purposes
        self.rewards_stats = {
            "frag": 0,
            "damage": 0,
            "ammo": 0,
            "health": 0,
            "armor": 0,
            "distance": 0,
        }

    def step(self, action, array=False):
        # Perform the action as usual
        state, reward, done, info = super().step(action)

        self._log_reward_stat("frag", reward)

        # Adjust the reward based on the shaping table
        if self.shaping:
            shaped_reward = reward + self.shape_rewards()
        else:
            shaped_reward = reward

        self.total_rew += shaped_reward

        return state, shaped_reward, done, info

    def reset(self):
        self._print_state()

        state = super().reset()

        self.last_health = 100
        self.last_x, self.last_y = self._get_player_pos()
        self.last_armor = self.last_frags = self.total_rew = self.deaths = 0

        # Damage count  is not cleared when starting a new episode: https://github.com/mwydmuch/ViZDoom/issues/399
        # self.last_damage_dealt = 0

        # Reset reward stats
        for k in self.rewards_stats.keys():
            self.rewards_stats[k] = 0

        return state

    def shape_rewards(self):
        reward_contributions = [
            self._compute_damage_reward(),
            self._compute_ammo_reward(),
            self._compute_health_reward(),
            self._compute_armor_reward(),
            self._compute_distance_reward(*self._get_player_pos()),
        ]

        return sum(reward_contributions)

    def _respawn_if_dead(self):
        if not self.game.is_episode_finished():
            # Check if player is dead
            if self.game.is_player_dead():
                self.deaths += 1
                self._reset_player()

    def _compute_distance_reward(self, x, y):
        """Computes a reward/penalty based on the distance travelled since last update."""
        dx = self.last_x - x
        dy = self.last_y - y

        distance = np.sqrt(dx**2 + dy**2)

        if distance - reward_threshold_distance > 0:
            reward = reward_factor_distance
        else:
            reward = -reward_factor_distance

        self.last_x = x
        self.last_y = y
        self._log_reward_stat("distance", reward)

        return reward

    def _compute_damage_reward(self):
        """Computes a reward based on total damage inflicted to enemies since last update."""
        damage_dealt = self.game.get_game_variable(GameVariable.DAMAGECOUNT)
        reward = reward_factor_damage * (damage_dealt - self.last_damage_dealt)

        self.last_damage_dealt = damage_dealt
        self._log_reward_stat("damage", reward)

        return reward

    def _compute_health_reward(self):
        """Computes a reward/penalty based on total health change since last update."""
        # When the player is dead, the health game variable can be -999900
        health = max(self.game.get_game_variable(GameVariable.HEALTH), 0)

        health_reward = reward_factor_health_increment * max(
            0, health - self.last_health
        )
        health_penalty = reward_factor_health_decrement * min(
            0, health - self.last_health
        )
        reward = health_reward - health_penalty

        self.last_health = health
        self._log_reward_stat("health", reward)

        return reward

    def _compute_armor_reward(self):
        """Computes a reward/penalty based on total armor change since last update."""
        armor = self.game.get_game_variable(GameVariable.ARMOR)
        reward = reward_factor_armor_increment * max(0, armor - self.last_armor)

        self.last_armor = armor
        self._log_reward_stat("armor", reward)

        return reward

    def _compute_ammo_reward(self):
        """Computes a reward/penalty based on total ammunition change since last update."""
        self.weapon_state = self._get_weapon_state()

        new_ammo_state = self._get_ammo_state()
        ammo_diffs = (new_ammo_state - self.ammo_state) * self.weapon_state
        ammo_reward = reward_factor_ammo_increment * max(0, np.sum(ammo_diffs))
        ammo_penalty = reward_factor_ammo_decrement * min(0, np.sum(ammo_diffs))
        reward = ammo_reward - ammo_penalty

        self.ammo_state = new_ammo_state
        self._log_reward_stat("ammo", reward)

        return reward

    def _get_player_pos(self):
        """Returns the player X- and Y- coordinates."""
        return self.game.get_game_variable(
            GameVariable.POSITION_X
        ), self.game.get_game_variable(GameVariable.POSITION_Y)

    def _get_ammo_state(self):
        """Returns the total available ammunition per weapon slot."""
        ammo_state = np.zeros(10)

        for i in range(10):
            ammo_state[i] = self.game.get_game_variable(AMMO_VARIABLES[i])

        return ammo_state

    def _get_weapon_state(self):
        """Returns which weapon slots can be used. Available weapons are encoded as ones."""
        weapon_state = np.zeros(10)

        for i in range(10):
            weapon_state[i] = self.game.get_game_variable(WEAPON_VARIABLES[i])

        return weapon_state

    def _log_reward_stat(self, kind: str, reward: float):
        self.rewards_stats[kind] += reward

    def _reset_player(self):
        self.last_health = 100
        self.last_armor = 0
        self.game.respawn_player()
        self.last_x, self.last_y = self._get_player_pos()
        self.ammo_state = self._get_ammo_state()

    def _print_state(self):
        super()._print_state()


REWARD_THRESHOLDS = [5, 10, 15, 20, 25, 25]


class DoomWithBotsCurriculum(DoomWithBotsShaped):
    def __init__(
        self,
        game,
        frame_processor,
        frame_skip,
        n_bots,
        shaping,
        initial_level=0,
        max_level=5,
        rolling_mean_length=10,
    ):
        super().__init__(game, frame_processor, frame_skip, n_bots, shaping)

        # Initialize ACS script difficulty level
        game.send_game_command("pukename change_difficulty 0")

        # Internal state
        self.level = initial_level
        self.max_level = max_level
        self.rolling_mean_length = rolling_mean_length
        self.last_rewards = deque(maxlen=rolling_mean_length)

    def step(self, action, array=False):
        # Perform action step as usual
        state, reward, done, infos = super().step(action, array)

        # After an episode, check whether difficulty should be increased.
        if done:
            self.last_rewards.append(self.total_rew)
            run_mean = np.mean(self.last_rewards)
            print(
                "Avg. last 10 runs of {}: {:.2f}. Current difficulty level: {}".format(
                    self.name, run_mean, self.level
                )
            )
            if (
                run_mean > REWARD_THRESHOLDS[self.level]
                and len(self.last_rewards) >= self.rolling_mean_length
            ):
                self._change_difficulty()

        return state, reward, done, infos

    def reset(self):
        state = super().reset()
        self.game.send_game_command(f"pukename change_difficulty {self.level}")

        return state

    def _change_difficulty(self):
        """Adjusts the difficulty by setting the difficulty level in the ACS script."""
        if self.level < self.max_level:
            self.level += 1
            print(f"Changing difficulty for {self.name} to {self.level}")
            self.game.send_game_command(f"pukename change_difficulty {self.level}")
            self.last_rewards = deque(maxlen=self.rolling_mean_length)
        else:
            print(f"{self.name} already at max level!")


def game_instance(scenario):
    """Creates a Doom game instance."""
    game = vizdoom.DoomGame()
    game.load_config(f"scenarios/{scenario}.cfg")
    game.add_game_args(envs.DOOM_ENV_WITH_BOTS_ARGS)
    # game.set_window_visible(False)
    game.init()

    return game


def env_with_bots_shaped(scenario, **kwargs) -> envs.DoomEnv:
    """Wraps a Doom game instance in an environment with shaped rewards."""
    game = game_instance(scenario)
    return DoomWithBotsShaped(game, **kwargs)


def vec_env_with_bots_shaped(n_envs=1, **kwargs) -> VecTransposeImage:
    """Wraps Doom game instances in a vectorized environment with shaped rewards using true parallelism."""
    return VecTransposeImage(
        SubprocVecEnv([lambda: env_with_bots_shaped(**kwargs) for _ in range(n_envs)])
    )


def dummy_vec_env_with_bots_shaped(n_envs=1, **kwargs) -> VecTransposeImage:
    """Wraps a Doom game instance in a vectorized environment with shaped rewards."""
    return VecTransposeImage(
        DummyVecEnv([lambda: env_with_bots_shaped(**kwargs)] * n_envs)
    )


def dummy_vec_env_with_bots_curriculum(n_envs=1, **kwargs) -> VecTransposeImage:
    """Wraps a Doom game instance in a vectorized environment with shaped rewards and curriculum."""
    scenario = kwargs.pop("scenario")  # Remove 'scenario' from kwargs
    return VecTransposeImage(
        DummyVecEnv(
            [lambda: DoomWithBotsCurriculum(game_instance(scenario), **kwargs)] * n_envs
        )
    )


def make_gif(agent, file_path, num_episodes=1):
    env = dummy_vec_env_with_bots_curriculum(1, **eval_env_args)

    images = []
    actions = []
    health_values = []  # New list to store health values
    for i in range(num_episodes):
        print(f"Episode {i+1} of {num_episodes}")
        obs = env.reset()

        done = False
        while not done:
            action, _ = agent.predict(obs)
            obs, _, done, _ = env.step(action)

            # Get the raw screen buffer from the Doom game instance
            screen = env.venv.envs[0].game.get_state().screen_buffer

            # Get the current health value
            health = env.venv.envs[0].game.get_game_variable(GameVariable.HEALTH)
            health_values.append(health)  # Store the health value

            actions.append(action)
            images.append(screen)

    print("Health values:", health_values)
    print("Number of health values:", len(health_values))
    print("Number of actions:", len(actions))
    print("Number of images:", len(images))

    imageio.mimsave(file_path, images, fps=20)
    env.close()
    print(f"GIF saved to {file_path}")

    return health_values  # Return the health values for further analysis if needed


def make_parquet(agent, parquet_path, num_episodes=1):
    env = dummy_vec_env_with_bots_curriculum(1, **eval_env_args)

    entries = []

    for episode in range(num_episodes):
        print(f"Episode {episode+1} of {num_episodes}")
        obs = env.reset()
        done = False

        episode_health = []
        episode_actions = []
        episode_images = []

        while not done:
            action, _ = agent.predict(obs)
            obs, _, done, _ = env.step(action)

            screen = env.venv.envs[0].game.get_state().screen_buffer
            health = env.venv.envs[0].game.get_game_variable(GameVariable.HEALTH)

            # Convert screen to PIL Image
            pil_img = pil_image.fromarray(screen)

            episode_health.append(int(health))
            episode_actions.append(
                int(action[0])
            )  # Assuming action is a 1-element array
            episode_images.append(pil_img)

        entries.append(
            {
                "sample_id": episode,
                "health": episode_health,
                "actions": episode_actions,
                "images": episode_images,
            }
        )

    # Define features for the dataset
    features = Features(
        {
            "sample_id": Value("int32"),
            "health": Sequence(Value("int32")),
            "actions": Sequence(Value("int32")),
            "images": Sequence(Image()),
        }
    )

    # Create the dataset
    dataset = Dataset.from_list(entries, features=features)

    # Save to Parquet
    dataset.to_parquet(parquet_path)

    print(f"Data saved to {parquet_path}")
    env.close()


MODEL_PATH = "/Users/pbowmandavis/Downloads/gameNgen-repro/stable_baselines_playground/rl-doom/standalone_examples/logs/models/dm_longrun_test/final_model.zip"

if __name__ == "__main__":
    scenario = "deathmatch_simple"

    env_args = {
        "scenario": scenario,
        "frame_skip": 2,
        "frame_processor": envs.default_frame_processor,
        "n_bots": 8,
        "shaping": True,
        "initial_level": 1,
        "max_level": 5,
        "rolling_mean_length": 10,
    }

    eval_env_args = dict(env_args)
    new_env = dummy_vec_env_with_bots_curriculum(1, **env_args)
    agent2 = envs.load_model(
        MODEL_PATH,
        new_env,
    )

    make_gif(agent2,"./testout.gif",num_episodes=1)
    # make_parquet(agent2,"./test.parquet",num_episodes=1)
