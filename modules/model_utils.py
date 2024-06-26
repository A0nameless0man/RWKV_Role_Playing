import copy
import torch
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.allow_tf32 = True
torch.backends.cuda.matmul.allow_tf32 = True
from rwkv.model import RWKV
from rwkv.utils import PIPELINE
import gc
from modules.mirostat import Mirostat

class ModelUtils:

  model = None
  pipline = None
  model_path = None
  strategy = None
  CHUNK_LEN = 100
  END_OF_TEXT = 0
  NEG_INF = -999999999
  AVOID_REPEAT = '，：？！'
  AVOID_REPEAT_TOKENS = []
  EXEMPT_TOKENS = [11, 34, 41, 42, 43, 45, 47, 59, 64, 575, 578, 579, 580, 581, 6884,
                   10080, 19126, 19133, 19134, 19137, 19151, 19156, 21214]
  all_state = {}
  miro = None

  def __init__(self, args):
    self.model_path = args.model
    self.strategy = args.strategy
    self.miro = Mirostat()

  def load_model(self):
    self.model = RWKV(model=self.model_path, strategy=self.strategy)
    self.pipeline = PIPELINE(self.model, "rwkv_vocab_v20230424")
    for i in self.AVOID_REPEAT:
      dd = self.pipeline.encode(i)
      assert len(dd) == 1
      self.AVOID_REPEAT_TOKENS += dd

  def run_rnn(self, model_tokens, model_state, tokens):
    tokens = [int(x) for x in tokens]
    model_tokens += tokens
    while len(tokens) > 0:
      out, model_state = self.model.forward(tokens[:self.CHUNK_LEN], model_state)
      tokens = tokens[self.CHUNK_LEN:]
    if model_tokens[-1] in self.AVOID_REPEAT_TOKENS:
      out[model_tokens[-1]] = self.NEG_INF
    return out, model_tokens, model_state
  
  def save_all_stat(self, name, last_out, model_tokens, model_state):
    n = f'{name}'
    self.all_state[n] = {
      'out': last_out,
      'rnn': copy.deepcopy(model_state),
      'token': copy.deepcopy(model_tokens)
    }

  def load_all_stat(self, name):
    n = f'{name}'
    model_state = copy.deepcopy(self.all_state[n]['rnn'])
    model_tokens = copy.deepcopy(self.all_state[n]['token'])
    return self.all_state[n]['out'], model_tokens, model_state
  
  def remove_stat(self, name):
    n = f'{name}'
    if n in self.all_state.keys():
      del self.all_state[n]
  
  def get_reply(self, model_tokens, model_state, out, chat_param, ban_token=[]):
    self.clear_cache()
    begin = len(model_tokens)
    out_last = begin
    occurrence = {}
    self.miro.set_param(chat_param['tau'], chat_param['lr'], 2 * chat_param['tau'])
    for i in range(300):
      if i > 20:
        out[261] += (i - 20) * 0.01
      for n in occurrence:
        if out[n] > 0:
          out[n] = out[n] / (1 + chat_param['presence_penalty'])
        else:
          out[n] = out[n] * (1 + chat_param['presence_penalty'])
        out[n] -= occurrence[n] * chat_param['frequency_penalty']
      for b in ban_token:
        if b not in self.EXEMPT_TOKENS:
          out[b] -= chat_param['context_penalty']
      if chat_param['tau']:
        token = self.miro.choise(out)
      else:
        token = self.pipeline.sample_logits(out, chat_param['temperature'], chat_param['top_p'], chat_param['top_k'])
      if token not in occurrence:
        occurrence[token] = 0
      else:
        occurrence[token] += 1
      out, model_tokens, model_state = self.run_rnn(model_tokens, model_state, [token])
      out[self.END_OF_TEXT] = self.NEG_INF
      xxx = self.pipeline.decode(model_tokens[out_last:])
      if '\ufffd' not in xxx: # avoid utf-8 display issues
        out_last = begin + i + 1
      send_msg = self.pipeline.decode(model_tokens[begin:])
      if '\n\n' in send_msg:
        send_msg = send_msg.strip()
        break
    return send_msg, out, model_tokens, model_state
  
  def format_chat_param(self, top_k, temperature, tau, lr, top_p, presence_penalty, frequency_penalty, 
                        context_penalty):
    chat_param = {
      'top_k': top_k,
      'temperature': temperature,
      'tau': tau,
      'lr': lr,
      'top_p': top_p,
      'presence_penalty': presence_penalty,
      'frequency_penalty': frequency_penalty,
      'context_penalty': context_penalty
    }
    return chat_param
  
  def clear_cache(self):
    gc.collect()
    torch.cuda.empty_cache()
  