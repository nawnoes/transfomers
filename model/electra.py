from collections import namedtuple

import torch
from torch import nn
from model.util import clones
import torch.nn.functional as F
from model.util import log, gumbel_sample, mask_with_tokens, prob_mask_like, get_mask_subset_with_prob
from model.transformer import PositionalEmbedding,Encoder
from transformers.activations import get_activation
from torch.nn import CrossEntropyLoss


Results = namedtuple('Results', [
  'loss',
  'mlm_loss',
  'disc_loss',
  'gen_acc',
  'disc_acc',
  'disc_labels',
  'disc_predictions'
])

class TransformerEncoderModel(nn.Module):
  def __init__(self, vocab_size, dim, emb_dim, max_seq_len, depth, head_num, dropout =0.1):
    super(TransformerEncoderModel,self).__init__()
    self.dim=dim
    self.emb_dim=emb_dim

    self.token_emb= nn.Embedding(vocab_size, emb_dim)
    self.position_emb = PositionalEmbedding(emb_dim, max_seq_len)
    self.encoders = clones(Encoder(d_model=dim, head_num=head_num, dropout=dropout), depth)
    self.norm = nn.LayerNorm(dim)

    if dim != emb_dim:
      self.embeddings_project = nn.Linear(emb_dim, dim)

  def get_input_embeddings(self):
      return self.token_emb

  def set_input_embeddings(self, value):
      self.token_emb = value

  def _tie_or_clone_weights(self, first_module, second_module):
    """ Tie or clone module weights depending of weither we are using TorchScript or not
    """
    if self.config.torchscript:
      first_module.weight = nn.Parameter(second_module.weight.clone())
    else:
      first_module.weight = second_module.weight

    if hasattr(first_module, 'bias') and first_module.bias is not None:
      first_module.bias.data = torch.nn.functional.pad(first_module.bias.data, (0, first_module.weight.shape[0] - first_module.bias.shape[0]),'constant',0)

  def forward(self, input_ids, input_mask):
    x = self.token_emb(input_ids)
    x = x + self.position_emb(input_ids).type_as(x)

    if self.emb_dim != self.dim:
      x = self.embeddings_project(x)

    for encoder in self.encoders:
      x = encoder(x, input_mask)
    x = self.norm(x)

    return x
class GeneratorHead(nn.Module):
  def __init__(self, vocab_size, dim, emb_dim, layer_norm_eps=1e-12):
    super().__init__()
    self.vocab_size = vocab_size

    self.dense = nn.Linear(dim, emb_dim)
    self.activation = F.gelu
    self.norm = nn.LayerNorm(emb_dim, eps=layer_norm_eps)
    self.decoder = nn.Linear(emb_dim, vocab_size, bias=False)
    self.bias = nn.Parameter(torch.zeros(vocab_size))

    # Need a link between the two variables so that the bias is correctly resized with `resize_token_embeddings`
    self.decoder.bias = self.bias

  def forward(self, hidden_states, masked_lm_labels=None):
    hidden_states = self.dense(hidden_states)
    hidden_states = self.activation(hidden_states)
    hidden_states = self.norm(hidden_states)

    logits = self.decoder(hidden_states)
    outputs = (logits,)

    if masked_lm_labels is not None:
      loss_fct = nn.CrossEntropyLoss()
      genenater_loss = loss_fct(logits.view(-1, self.vocab_size), masked_lm_labels.view(-1))
      outputs += (genenater_loss,)
    return outputs

class DiscriminatorHead(nn.Module):
  def __init__(self, dim, layer_norm_eps=1e-12):
    super().__init__()
    self.dense = nn.Linear(dim, dim)
    self.activation = F.gelu
    self.LayerNorm = nn.LayerNorm(dim, eps=layer_norm_eps)
    self.classifier = nn.Linear(dim, 1)

  def forward(self, hidden_states,is_replaced_label = None):
    hidden_states = self.dense(hidden_states)
    hidden_states = self.activation(hidden_states)
    hidden_states = self.LayerNorm(hidden_states)
    logits = self.classifier(hidden_states)

    outputs = (logits,)

    if is_replaced_label is not None:
      loss_fct = nn.BCEWithLogitsLoss()
      discriminator_loss = loss_fct(logits.view(-1), is_replaced_label.view(-1))
      outputs += (discriminator_loss,)

    return outputs

class Electra(nn.Module):
  def __init__(self,
               config,
               gen_config,
               disc_config,
               num_tokens,
               mask_token_id,
               pad_token_id,
               mask_ignore_token_ids,
               mask_prob=0.15,
               replace_prob=0.85,
               random_token_prob=0.,
               disc_weight=50.,
               gen_weight=1.,
               temperature=1.):
    super().__init__()
    # Electra Generator
    self.generator = TransformerEncoderModel(vocab_size=num_tokens,
                                             max_seq_len=config.max_seq_len,
                                             dim=gen_config.dim,
                                             emb_dim=gen_config.emb_dim,
                                             depth=gen_config.depth,
                                             head_num=gen_config.head_num)
    self.generator_head = GeneratorHead(vocab_size=num_tokens,
                                        dim=gen_config.dim,
                                        emb_dim=gen_config.emb_dim)
    # Electra Discriminator
    self.discriminator = TransformerEncoderModel(vocab_size=num_tokens,
                                             max_seq_len=config.max_seq_len,
                                             dim=disc_config.dim,
                                             emb_dim=disc_config.emb_dim,
                                             depth=disc_config.depth,
                                             head_num=disc_config.head_num)
    self.discriminator_head = DiscriminatorHead(dim=disc_config.dim)

    # mlm probabilities
    self.mask_prob = mask_prob
    self.replace_prob = replace_prob

    self.num_tokens = num_tokens
    self.random_token_prob = random_token_prob

    # token ids
    self.pad_token_id = pad_token_id
    self.mask_token_id = mask_token_id
    self.mask_ignore_token_ids = set([*mask_ignore_token_ids, pad_token_id])

    # sampling temperature
    self.temperature = temperature

    # loss weights
    self.disc_weight = disc_weight
    self.gen_weight = gen_weight

  def tie_embedding_weight(self):
    # 4.2 weight tie the token and positional embeddings of generator and discriminator
    # 제너레이터와 디스크리미네이터의 토큰, 포지션 임베딩을 공유한다(tie).
    self.generator.token_emb = self.discriminator.token_emb
    self.generator.position_emb = self.discriminator.position_emb

  def forward(self, input, input_mask):
    b, t = input.shape

    replace_prob = prob_mask_like(input, self.replace_prob)

    # do not mask [pad] tokens, or any other tokens in the tokens designated to be excluded ([cls], [sep])
    # also do not include these special tokens in the tokens chosen at random
    no_mask = mask_with_tokens(input, self.mask_ignore_token_ids)
    mask = get_mask_subset_with_prob(~no_mask, self.mask_prob)

    # get mask indices
    # 마스크의 인덱스를 가져옴
    mask_indices = torch.nonzero(mask, as_tuple=True)

    # mask input with mask tokens with probability of `replace_prob` (keep tokens the same with probability 1 - replace_prob)
    masked_input = input.clone().detach()

    # if random token probability > 0 for mlm
    if self.random_token_prob > 0:
      assert self.num_tokens is not None, 'Number of tokens (num_tokens) must be passed to Electra for randomizing tokens during masked language modeling'

      random_token_prob = prob_mask_like(input, self.random_token_prob)
      random_tokens = torch.randint(0, self.num_tokens, input.shape, device=input.device)
      random_no_mask = mask_with_tokens(random_tokens, self.mask_ignore_token_ids)
      random_token_prob &= ~random_no_mask
      random_indices = torch.nonzero(random_token_prob, as_tuple=True)
      masked_input[random_indices] = random_tokens[random_indices]

    # [mask] input
    masked_input = masked_input.masked_fill(mask * replace_prob, self.mask_token_id)

    # set inverse of mask to padding tokens for labels
    gen_labels = input.masked_fill(~mask, self.pad_token_id)

    # get generator output and get mlm loss
    gen_output = self.generator(input_ids=masked_input, input_mask=input_mask)
    logits, mlm_loss = self.generator_head(gen_output, masked_lm_labels=gen_labels)
    # nn.CrossEntropyLoss()(logits[mask_indices].view(-1,22000),gen_labels[mask_indices])
    # 위 함수로 loss를 해도 동일
    # mlm_loss = F.cross_entropy(
    #   logits.transpose(1, 2),
    #   gen_labels,
    #   ignore_index=self.pad_token_id
    # )

    # use mask from before to select logits that need sampling
    sample_logits = logits[mask_indices]

    # sample
    sampled = gumbel_sample(sample_logits, temperature=self.temperature)

    # scatter the sampled values back to the input
    disc_input = input.clone()
    disc_input[mask_indices] = sampled.detach()

    # generate discriminator labels, with replaced as True and original as False
    disc_labels = (input != disc_input).float().detach()

    # get discriminator predictions of replaced / original
    non_padded_indices = torch.nonzero(input != self.pad_token_id, as_tuple=True)

    # get discriminator output and binary cross entropy loss
    disc_ouput = self.discriminator(input_ids=disc_input,input_mask=input_mask)
    disc_logits, disc_loss = self.discriminator_head(hidden_states= disc_ouput, is_replaced_label=disc_labels)
    # disc_logits = disc_logits.reshape_as(disc_labels)
    #
    # disc_loss = F.binary_cross_entropy_with_logits(
    #   disc_logits[non_padded_indices],
    #   disc_labels[non_padded_indices]
    # )

    # gather metrics
    with torch.no_grad():
      gen_predictions = torch.argmax(logits, dim=-1)
      disc_predictions = torch.round((torch.sign(disc_logits) + 1.0) * 0.5)
      gen_acc = (gen_labels[mask] == gen_predictions[mask]).float().mean()
      disc_acc = 0.5 * (disc_labels[mask] == disc_predictions[mask]).float().mean() + 0.5 * (
        disc_labels[~mask] == disc_predictions[~mask]).float().mean()

    # return weighted sum of losses
    return Results(self.gen_weight * mlm_loss + self.disc_weight * disc_loss, mlm_loss, disc_loss, gen_acc, disc_acc,
                   disc_labels, disc_predictions)

class ElectraMRCHead(nn.Module):
  def __init__(self, dim, num_labels,hidden_dropout_prob=0.3):
    super().__init__()
    self.dense = nn.Linear(dim, 1*dim)
    self.dropout = nn.Dropout(hidden_dropout_prob)
    self.out_proj = nn.Linear(1*dim,num_labels)

  def forward(self, x, **kwargs):
    # x = features[:, 0, :]  # take <s> token (equiv. to [CLS])
    x = self.dropout(x)
    x = self.dense(x)
    x = get_activation("gelu")(x)  # although BERT uses tanh here, it seems Electra authors used gelu here
    x = self.dropout(x)
    x = self.out_proj(x)
    return x

class ElectraMRCModel(nn.Module):
  def __init__(self, electra, dim, num_labels=2, causal=False, dropout_prob=0.2):
    super().__init__()
    self.electra = electra
    self.mrc_head = ElectraMRCHead(dim, num_labels)

  def forward(self,
              input_ids=None,
              input_mask=None,
              start_positions=None,
              end_positions=None,
              **kwargs):
    # 1. reformer의 출력
    outputs = self.electra(input_ids, input_mask)

    # 2. mrc를 위한
    logits = self.mrc_head(outputs)

    start_logits, end_logits = logits.split(1, dim=-1)
    start_logits = start_logits.squeeze(-1)
    end_logits = end_logits.squeeze(-1)

    if start_positions is not None and end_positions is not None:
      # If we are on multi-GPU, split add a dimension
      if len(start_positions.size()) > 1:
        start_positions = start_positions.squeeze(-1)
      if len(end_positions.size()) > 1:
        end_positions = end_positions.squeeze(-1)
      # sometimes the start/end positions are outside our model inputs, we ignore these terms
      ignored_index = start_logits.size(1)
      start_positions.clamp_(0, ignored_index)
      end_positions.clamp_(0, ignored_index)

      loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
      start_loss = loss_fct(start_logits, start_positions)
      end_loss = loss_fct(end_logits, end_positions)
      total_loss = (start_loss + end_loss) / 2
      return total_loss
    else:
      return start_logits, end_logits