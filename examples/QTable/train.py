#!/usr/bin/env python3
import os
import time
import wandb
import argparse
from tqdm import tqdm

from mivp_agent.bridge import ModelBridgeServer
from mivp_agent.util.display import ModelConsole
from mivp_agent.util.parse import parse_report
from mivp_agent.util.math import dist
from mivp_agent.aquaticus.const import FIELD_BLUE_FLAG

from model.util.constants import LEARNING_RATE, DISCOUNT, EPISODES
from model.util.constants import FIELD_RESOLUTION
from model.util.constants import EPSILON_START, EPSILON_DECAY_START, EPSILON_DECAY_AMT, EPSILON_DECAY_END
from model.util.constants import ACTIONS, ACTION_SPACE_SIZE
from model.util.constants import REWARD_SUCCESS, REWARD_FAILURE, REWARD_STEP
from model.util.constants import SAVE_DIR, SAVE_EVERY
from model.model import QLearn

MNGR_STATE = 'EPISODE_MNGR_STATE'
MNGR_REPORT = 'EPISODE_MNGR_REPORT'

PAUSE_INSTR = {
    'speed': 0.0,
    'course': 0.0,
    'posts': { # This will only be sent the first time, after that it is turrned off
        'EPISODE_MNGR_CTRL': 'type=hardstop'
    },
    'ctrl_msg': 'PAUSE'
}

def train(args, config):
  # Setup model
  q = QLearn(
    lr=config['lr'],
    gamma=config['gamma'],
    action_space_size=config['action_space_size'],
    field_res=config['field_res'],
    verbose=args.debug,
    save_dir=args.save_dir
  )

  # Start connection to sim
  with ModelBridgeServer() as server:
    print('Waiting for sim connection...')
    server.accept()
    
    # ---------------------------------------
    # Part 1: Asserting simulation state

    # Create instruction object
    instr = {
      'speed': 0.0,
      'course': 0.0,
      'posts': {},
      'ctrl_msg': 'SEND_STATE'
    }
    
    # Ask BHV_Agent for first state
    server.send_instr(instr)
    MOOS_STATE = server.listen_state()
    if MOOS_STATE[MNGR_STATE] != 'PAUSED':
      tqdm.write('Waiting for pEpisodeManager...')
    
    # Read state from BHV_Agent util pEpisode
    while MOOS_STATE[MNGR_STATE] != 'PAUSED':
      server.send_instr(instr)
      MOOS_STATE = server.listen_state()

    # ---------------------------------------
    # Part 2: Local state initalization
    episode_count = 0
    last_episode_num = None
    epsilon = config['epsilon_start']

    min_dist = None
    last_state = None
    current_action = None
    episode_reward = 0
    progress_bar = tqdm(total=config['episodes'], desc='Training')

    # Debugging stuff
    last_MOOS_time = None
    loop_times = []

    # ---------------------------------------
    # Part 3: Running the episodes
    instr['posts'] = {
        'EPISODE_MNGR_CTRL': 'type=start'
    }
    server.send_instr(instr)
    while episode_count < config['episodes']:
      # Listen for state
      MOOS_STATE = server.listen_state()
      MOOS_STATE[MNGR_REPORT] = parse_report(MOOS_STATE[MNGR_REPORT])
      
      # Debugging stuff
      if last_MOOS_time is not None:
        loop_times.append(MOOS_STATE['HELM_TIME']-last_MOOS_time)
      last_MOOS_time = MOOS_STATE['HELM_TIME']

      # Detect state transitions
      model_state = q.get_state(
        MOOS_STATE['NAV_X'],
        MOOS_STATE['NAV_Y'],
        MOOS_STATE['NODE_REPORTS']['evan']['NAV_X'],
        MOOS_STATE['NODE_REPORTS']['evan']['NAV_Y']
      )

      # Detect state transitions & do updates
      if model_state != last_state:
        # Default reward
        reward = config['reward_step']

        # Detect new episodes
        if MOOS_STATE[MNGR_REPORT] is None:
          # Sanity check
          assert episode_count == 0
        elif last_episode_num != MOOS_STATE[MNGR_REPORT]['EPISODE']:
          # Check for bad episodes
          if MOOS_STATE[MNGR_REPORT]['DURATION'] > 2:
            # If succeeded, set q value
            if MOOS_STATE[MNGR_REPORT]['SUCCESS']:
              episode_reward += config['reward_success']
              q.set_qvalue(last_state, current_action, config['reward_success'])
            else:
              # -------------------------------
              # TODO: This part I have never done before... not sure how it will handle
              # -------------------------------
              episode_reward += config['reward_failure']
              q.set_qvalue(last_state, current_action, config['reward_failure'])
            
            # Log important information
            console_report = f"Episode: {episode_count}"
            console_report += f", Reward: {episode_reward}"
            console_report += f", Duration: {round(MOOS_STATE[MNGR_REPORT]['DURATION'],2)}"
            console_report += f", Success: {MOOS_STATE[MNGR_REPORT]['SUCCESS']}"
            console_report += f", Min Dist: {round(min_dist, 2)}"
            console_report += f", Epsilon: {round(epsilon, 4)}"
            console_report += f", Avg Delta: {round(sum(loop_times)/len(loop_times),2)}"
            tqdm.write(console_report)

            if args.wandb_key is not None:
              wandb.log({
                'episode': episode_count,
                'reward': episode_reward,
                'epsilon': epsilon,
                'duration': round(MOOS_STATE[MNGR_REPORT]['DURATION'],2),
                'success': MOOS_STATE[MNGR_REPORT]['SUCCESS'],
                'min_dist': round(min_dist, 2),
                'avg_delta': round(sum(loop_times)/len(loop_times),2),
              })

            # Update vars
            episode_count += 1
            progress_bar.update(1)
            # Decay e greedy
            if config['epsilon_decay_end'] >= episode_count >= config['epsilon_decay_start']:
              epsilon -= config['epsilon_decay_amt']
            # Save model
            if episode_count % SAVE_EVERY == 0:
              q.save(config['actions'], name=f'episode_{episode_count}')

          # Reset vars regardless of good or bad episode
          last_state = None
          last_episode_num = MOOS_STATE[MNGR_REPORT]['EPISODE']
          min_dist = None
          episode_reward = 0
          last_MOOS_time = None
          loop_times.clear()

        if last_state != None:
          # Do qtable updating for last state / action
          q.update_table(last_state, current_action, reward, model_state)
          episode_reward += reward

        # Choose new action for this state
        current_action = q.get_action(model_state, e=epsilon)

        # Update previous state 
        last_state = model_state

      # Construct instruction for BHV_Agent
      instr['speed'] = config['actions'][current_action]['speed']
      instr['course'] = config['actions'][current_action]['course']
      instr['posts'] = {}

      flag_dist = abs(dist((MOOS_STATE['NAV_X'], MOOS_STATE['NAV_Y']), FIELD_BLUE_FLAG))
      if flag_dist < 10:
        instr['posts']['FLAG_GRAB_REQUEST'] = f'vname={MOOS_STATE["VNAME"]}'
      
      # Send instruction
      server.send_instr(instr)

      # Store min dist for reasons
      if min_dist is None or min_dist > flag_dist:
        min_dist = flag_dist


if __name__ == '__main__':
  save_dir = os.path.join(SAVE_DIR, str(round(time.time())))

  parser = argparse.ArgumentParser()
  parser.add_argument('--debug', action='store_true')
  parser.add_argument('--save_dir', default=save_dir)
  parser.add_argument('--wandb_key', default=None)
  
  args = parser.parse_args()

  # Construct config
  config = {
    'lr': LEARNING_RATE,
    'gamma': DISCOUNT,
    'episodes': EPISODES,
    'epsilon_start': EPSILON_START,
    'epsilon_decay_start': EPSILON_DECAY_START,
    'epsilon_decay_amt': EPSILON_DECAY_AMT,
    'epsilon_decay_end': EPSILON_DECAY_END,
    'field_res': FIELD_RESOLUTION,
    'actions': ACTIONS,
    'action_space_size': ACTION_SPACE_SIZE,
    'reward_success': REWARD_SUCCESS,
    'reward_failure': REWARD_FAILURE,
    'reward_step': REWARD_STEP,
  }

  if args.wandb_key is None:
    train(args, config)
  else:
    wandb.login(key=args.wandb_key)
    with wandb.init(project='mivp_agent_qtable', config=config):
      config = wandb.config
      train(args, config)