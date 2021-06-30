# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import logging
import torch
from .weight_masker import WeightMasker

__all__ = ['L1WeightHeadMasker', 'L2WeightHeadMasker']

logger = logging.getLogger('torch transformer head pruners')


class AttentionHeadMasker(WeightMasker):
    """
    A structured pruning masker base class that prunes convolutional layer filters.

    Parameters
    ----------
    model: nn.Module
        model to be pruned
    pruner: Pruner
        A Pruner instance used to prune the model
    head_hidden_dim: int
        Hidden dimension for each attention head (e.g., 64 for BERT base)
    """
    def __init__(self, model, pruner, head_hidden_dim=None):
        super().__init__(model, pruner)
        self.head_hidden_dim = head_hidden_dim
        assert self.head_hidden_dim is not None, "head_hidden_dim must be specified."

    def calc_mask(self, sparsity, wrapper, wrapper_idx=None, **depen_kwargs):
        """
        calculate the mask for `wrapper`.

        Parameters
        ----------
        sparsity: float/list of float
            The target sparsity of the wrapper. If we calculate the mask in
            the normal way, then sparsity is a float number. In contrast, if
            we calculate the mask in the dependency-aware way, sparsity is a
            list of float numbers, each float number corressponds to a sparsity
            of a layer.
        wrapper: PrunerModuleWrapper/list of PrunerModuleWrappers
            The wrapper of the target layer. If we calculate the mask in the normal
            way, then `wrapper` is an instance of PrunerModuleWrapper, else `wrapper`
            is a list of PrunerModuleWrapper.
        wrapper_idx: int/list of int
            The index of the wrapper.
        Returns
        -------
        dict
            dictionary for storing masks, keys of the dict:
            'weight_mask':  weight mask tensor
            'bias_mask': bias mask tensor (optional)
        """
        mask, weight, num_prune = self._get_current_state(sparsity, wrapper, wrapper_idx)
        num_total = weight.size(0) // self.head_hidden_dim
        if num_total < 2 or num_prune < 1:
            return mask
        return self.get_mask(mask, weight, num_prune, wrapper, wrapper_idx, **depen_kwargs)

    def _get_current_state(self, sparsity, wrapper, wrapper_idx=None):
        """
        Some pruner may prune the layers in a iterative way. In each pruning iteration,
        we may get the current state of this wrapper/layer, and continue to prune this layer
        based on the current state. This function is to get the current pruning state of the
        target wrapper/layer.
        Parameters
        ----------
        sparsity: float
            pruning ratio,  preserved weight ratio is `1 - sparsity`
        wrapper: PrunerModuleWrapper
            layer wrapper of this layer
        wrapper_idx: int
            index of this wrapper in pruner's all wrappers
        Returns
        -------
        base_mask: dict
            dict object that stores the mask of this wrapper in this iteration, if it is the
            first iteration, then we create a new mask with all ones. If there is already a
            mask in this wrapper, then we return the existing mask.
        weight: tensor
            the current weight of this layer
        num_prune: int
            how many heads we should prune
        """
        msg = 'module type {} is not supported!'.format(wrapper.type)
        assert wrapper.type == 'Linear', msg
        weight = wrapper.module.weight.data
        bias = None
        if hasattr(wrapper.module, 'bias') and wrapper.module.bias is not None:
            bias = wrapper.module.bias.data

        if wrapper.weight_mask is None:
            mask_weight = torch.ones(weight.size()).type_as(weight).detach()
        else:
            mask_weight = wrapper.weight_mask.clone()
        if bias is not None:
            if wrapper.bias_mask is None:
                mask_bias = torch.ones(bias.size()).type_as(bias).detach()
            else:
                mask_bias = wrapper.bias_mask.clone()
        else:
            mask_bias = None
        mask = {'weight_mask': mask_weight, 'bias_mask': mask_bias}

        num_total = weight.size(0) // self.head_hidden_dim
        num_prune = int(num_total * sparsity)

        # weight*mask_weight: apply base mask for iterative pruning
        return mask, weight * mask_weight, num_prune

    def get_mask(self, base_mask, weight, num_prune, wrapper, wrapper_idx, **depen_kwargs):
        """
        Calculate the mask of given layer.

        Parameters
        ----------
        base_mask: dict
            The basic mask with the same shape of weight, all item in the basic mask is 1.
        weight: tensor
            the module weight to be pruned
        num_prune: int
            Num of heads to prune
        wrapper: PrunerModuleWrapper
            layer wrapper of this layer
        wrapper_idx: int
            index of this wrapper in pruner's all wrappers
        Returns
        -------
        dict
            dictionary for storing masks
        """
        raise NotImplementedError('{} get_mask is not implemented'.format(self.__class__.__name__))

    def get_mask_by_importance_ranking(self, base_mask, weight, num_prune, wrapper, wrapper_idx, weight_group=None):
        """
        Calculate the mask of given layer by pruning out heads with lowest importance scores.

        Parameters
        ----------
        weight_group: list
            list of a group of weights for an attention layer
        base_mask: dict
            The basic mask with the same shape of weight, all item in the basic mask is 1.
        weight: tensor
            the module weight to be pruned
        num_prune: int
            Num of heads to prune
        wrapper: PrunerModuleWrapper
            layer wrapper of this layer
        wrapper_idx: int
            index of this wrapper in pruner's all wrappers
        Returns
        -------
        dict
            dictionary for storing masks
        """
        device = weight.device

        importance_scores = self.get_head_importance_scores(wrapper, weight_group, wrapper_idx)
        threshold = torch.topk(importance_scores, num_prune, largest=False)[0].max()

        # get q_proj, k_proj, v_proj, output_proj from the same attention head
        q_proj, k_proj, v_proj, output_proj = weight_group if weight_group is not None else \
            self.pruner.masking_groups[wrapper.group_idx]

        n_heads = q_proj.module.weight.size()[0] // self.head_hidden_dim
        weight_mask_shape = q_proj.module.weight.data.view([n_heads, -1]).size()
        bias_mask_shape = q_proj.module.bias.data.view([n_heads, -1]).size()

        mask_weight = torch.gt(importance_scores, threshold).unsqueeze(-1).expand(weight_mask_shape).type_as(weight)
        mask_bias = torch.gt(importance_scores, threshold).unsqueeze(-1).expand(bias_mask_shape).type_as(weight)

        mask_weight_proj = mask_weight.view(weight.size()).detach().to(device)
        mask_bias_proj = mask_bias.view(-1).detach().to(device) \
            if base_mask['bias_mask'] is not None else None
        masks_for_proj = {'weight_mask': mask_weight_proj.detach(), 'bias_mask': mask_bias_proj}

        mask_weight_dense = mask_bias_proj.expand_as(output_proj.module.weight.data).detach().to(device)
        mask_bias_dense = torch.ones_like(output_proj.module.bias.data).to(device)
        masks_for_dense = {'weight_mask': mask_weight_dense.detach(), 'bias_mask': mask_bias_dense}

        masks = [masks_for_proj, masks_for_proj, masks_for_proj, masks_for_dense]
        return masks

    def get_head_importance_scores(self, wrapper, weight_group, wrapper_idx):
        """
        Calculate the importance score for each head.
        Parameters
        ----------
        weight_group: list
            list of a group of weights for an attention layer
        wrapper: PrunerModuleWrapper
            layer wrapper of this layer
        wrapper_idx: int
            index of this wrapper in pruner's all wrappers
        Returns
        -------
        tensor
            Tensor that indicates the importance of each head
        """
        raise NotImplementedError('{} get_channel_sum is not implemented'.format(self.__class__.__name__))


class L1WeightHeadMasker(AttentionHeadMasker):
    """
    A structured pruning algorithm that prunes the heads weight smallest weight magnitude for the query, head,
    and key projection matrices. L1 norm is used for magnitude calculation. Note that in this implementation, weight
    norms of q_proj, k_proj, v_proj from each head are summed as the final importance score for the head.
    """
    def get_head_importance_scores(self, wrapper, weight_group, wrapper_idx):
        print('calculating importance scores for wrapper', wrapper.name)
        q_proj, k_proj, v_proj, _ = weight_group

        n_heads = q_proj.module.weight.size()[0] // self.head_hidden_dim
        query_proj_weights = q_proj.module.weight.data.view([n_heads, -1])
        key_proj_weights = k_proj.module.weight.data.view([n_heads, -1])
        value_proj_weights = v_proj.module.weight.data.view([n_heads, -1])

        query_norm_avg = torch.sum(torch.abs(query_proj_weights), -1)
        key_norm_avg = torch.sum(torch.abs(key_proj_weights), -1)
        value_norm_avg = torch.sum(torch.abs(value_proj_weights), -1)

        return ((query_norm_avg + key_norm_avg + value_norm_avg) / 3).detach()

    def get_mask(self, base_mask, weight, num_prune, wrapper, wrapper_idx, weight_group=None):
        return self.get_mask_by_importance_ranking(base_mask, weight, num_prune, wrapper, wrapper_idx, weight_group)


class L2WeightHeadMasker(AttentionHeadMasker):
    """
    A structured pruning algorithm that prunes the heads weight smallest weight magnitude for the query, head,
    and key projection matrices. L2 norm is used for magnitude calculation. Note that in this implementation, weight
    norms of q_proj, k_proj, v_proj from each head are summed as the final importance score for the head.
    """
    def get_head_importance_scores(self, wrapper, weight_group, wrapper_idx):
        q_proj, k_proj, v_proj, _ = weight_group

        n_heads = q_proj.module.weight.size()[0] // self.head_hidden_dim
        query_proj_weights = q_proj.module.weight.data.view([n_heads, -1])
        key_proj_weights = k_proj.module.weight.data.view([n_heads, -1])
        value_proj_weights = v_proj.module.weight.data.view([n_heads, -1])

        query_norm_avg = torch.sum(query_proj_weights ** 2, -1)
        key_norm_avg = torch.sum(key_proj_weights ** 2, -1)
        value_norm_avg = torch.sum(value_proj_weights ** 2, -1)

        return ((query_norm_avg + key_norm_avg + value_norm_avg) / 3).detach()

    def get_mask(self, base_mask, weight, num_prune, wrapper, wrapper_idx, weight_group=None):
        return self.get_mask_by_importance_ranking(base_mask, weight, num_prune, wrapper, wrapper_idx, weight_group)