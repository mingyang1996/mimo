import torch
import torch.nn as nn
from torch.autograd import Variable

from mimo.model.components.models import MimoTransformer
from mimo.model.decode import Beam


class GenerationModel(object):
    def __init__(self, model, beam_size, n_best, cuda=None):
        self.model = model
        self.cuda = torch.cuda.is_available() if cuda is None else cuda
        self.tt = torch.cuda if self.cuda else torch
        self.beam_size = beam_size
        self.n_best = n_best

        for k in model.decoders.keys():
            prob_projection = nn.LogSoftmax(dim=1)
            if self.cuda:
                prob_projection.cuda()
            else:
                prob_projection.cpu()
            model.decoders[k]['prob_projection'] = prob_projection
            model.decoders[k]['model'].eval()

        self.model = model
        self.model.eval()

    @classmethod
    def load(cls, path, cuda=None, **kwargs):
        print('Loading model: ' + path + ' ...')
        cuda = torch.cuda.is_available() if cuda is None else cuda
        if cuda:
            checkpoint = torch.load(path)
        else:
            checkpoint = torch.load(path, map_location=lambda storage, loc: storage)

        opt = checkpoint['settings']

        model = MimoTransformer(
            opt.src_vocab_size,
            opt.max_token_src_seq_len,
            checkpoint['config']['decoders'],
            proj_share_weight=opt.proj_share_weight,
            embs_share_weight=opt.embs_share_weight,
            d_model=opt.d_model,
            d_word_vec=opt.d_word_vec,
            d_inner_hid=opt.d_inner_hid,
            n_layers=opt.n_layers,
            n_head=opt.n_head,
            dropout=opt.dropout)

        model.load_state_dict(checkpoint['model'])
        print('Loaded model: ' + path)

        if cuda:
            model.cuda()
        else:
            model.cpu()

        return GenerationModel(model=model, **kwargs)

    def translate_batch(self, src_batch, batch_inits=None):
        results = {}

        # batch size is in different location depending on data
        for k in self.model.decoders.keys():
            decoder = self.model.decoders[k]['model']

            src_seq, src_pos = src_batch
            batch_size = src_seq.size(0)
            beam_size = self.beam_size
            if batch_inits is None:
                batch_inits = [int(src_seq[i][0]) for i in range(batch_size)]
            else:
                assert len(batch_inits) == batch_size

            # encode
            enc_output, *_ = self.model.encoder(src_seq, src_pos)

            # repeat data for beam
            src_seq = Variable(
                src_seq.data.repeat(1, beam_size).view(
                    src_seq.size(0) * beam_size, src_seq.size(1)))

            enc_output = Variable(
                enc_output.data.repeat(1, beam_size, 1).view(
                    enc_output.size(0) * beam_size, enc_output.size(1), enc_output.size(2)))

            # prepare beams
            beams = [Beam(beam_size, self.cuda, init=batch_inits[i]) for i in range(batch_size)]
            beam_inst_idx_map = {
                beam_idx: inst_idx for inst_idx, beam_idx in enumerate(range(batch_size))}
            n_remaining_sents = batch_size

            # decode
            for i in range(decoder.n_max_seq):
                len_dec_seq = i + 1

                # preparing decoded data seq
                # size: batch x beam x seq
                dec_partial_seq = torch.stack([b.get_current_state() for b in beams if not b.done])
                # size: (batch * beam) x seq
                dec_partial_seq = dec_partial_seq.view(-1, len_dec_seq)
                # wrap into a Variable
                dec_partial_seq = Variable(dec_partial_seq, volatile=True)

                # preparing decoded pos seq
                # size: 1 x seq
                dec_partial_pos = torch.arange(1, len_dec_seq + 1).unsqueeze(0)
                # size: (batch * beam) x seq
                dec_partial_pos = dec_partial_pos.repeat(n_remaining_sents * beam_size, 1)
                # wrap into a Variable
                dec_partial_pos = Variable(dec_partial_pos.type(torch.LongTensor), volatile=True)

                if self.cuda:
                    dec_partial_seq = dec_partial_seq.cuda()
                    dec_partial_pos = dec_partial_pos.cuda()

                # decoding
                dec_output, *_ = decoder(dec_partial_seq, dec_partial_pos, src_seq, enc_output)
                dec_output = dec_output[:, -1, :]  # (batch * beam) * d_model
                dec_output = decoder.tgt_word_proj(dec_output)
                out = self.model.decoders[k]['prob_projection'](dec_output)

                # batch x beam x n_words
                word_lk = out.view(n_remaining_sents, beam_size, -1).contiguous()

                active_beam_idx_list = []
                for beam_idx in range(batch_size):
                    if beams[beam_idx].done:
                        continue

                    inst_idx = beam_inst_idx_map[beam_idx]
                    if not beams[beam_idx].advance(word_lk.data[inst_idx]):
                        active_beam_idx_list += [beam_idx]

                if not active_beam_idx_list:
                    # all instances have finished their path to <EOS>
                    break

                # in this section, the sentences that are still active are
                # compacted so that the decoder is not run on completed sentences
                active_inst_idxs = self.tt.LongTensor([beam_inst_idx_map[k] for k in active_beam_idx_list])

                # update the idx mapping
                beam_inst_idx_map = {beam_idx: inst_idx for inst_idx, beam_idx in enumerate(active_beam_idx_list)}

                def update_active_seq(seq_var, active_inst_idxs):
                    """ Remove the src sequence of finished instances in one batch. """

                    inst_idx_dim_size, *rest_dim_sizes = seq_var.size()
                    inst_idx_dim_size = inst_idx_dim_size * len(active_inst_idxs) // n_remaining_sents
                    new_size = (inst_idx_dim_size, *rest_dim_sizes)

                    # select the active instances in batch
                    original_seq_data = seq_var.data.view(n_remaining_sents, -1)
                    active_seq_data = original_seq_data.index_select(0, active_inst_idxs)
                    active_seq_data = active_seq_data.view(*new_size)

                    return Variable(active_seq_data, volatile=True)

                def update_active_enc_info(enc_info_var, active_inst_idxs):
                    """ Remove the encoder outputs of finished instances in one batch. """

                    inst_idx_dim_size, *rest_dim_sizes = enc_info_var.size()
                    inst_idx_dim_size = inst_idx_dim_size * len(active_inst_idxs) // n_remaining_sents
                    new_size = (inst_idx_dim_size, *rest_dim_sizes)

                    # select the active instances in batch
                    original_enc_info_data = enc_info_var.data.view(n_remaining_sents, -1, self.model.d_model)
                    active_enc_info_data = original_enc_info_data.index_select(0, active_inst_idxs)
                    active_enc_info_data = active_enc_info_data.view(*new_size)

                    return Variable(active_enc_info_data, volatile=True)

                src_seq = update_active_seq(src_seq, active_inst_idxs)
                enc_output = update_active_enc_info(enc_output, active_inst_idxs)

                # update the remaining size
                n_remaining_sents = len(active_inst_idxs)

            # return some useful information
            all_hyp, all_scores = [], []
            n_best = self.n_best

            for beam_idx in range(batch_size):
                scores, tail_idxs = beams[beam_idx].sort_scores()
                all_scores += [scores[:n_best]]

                hyps = [beams[beam_idx].get_hypothesis(i) for i in tail_idxs[:n_best]]
                all_hyp += [hyps]

            results[k] = all_hyp, all_scores

        return results