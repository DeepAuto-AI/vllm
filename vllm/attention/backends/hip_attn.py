"""
Attention Layer with HiPAttention.

DeepAuto-AI @ 2024, Written by Heejun Lee and Geon Park
"""
import copy
from dataclasses import dataclass
import os
import random
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type
from dataclasses import dataclass
import warnings

import torch
from vllm_flash_attn import flash_attn_varlen_func, flash_attn_with_kvcache
from hip import (
    paged_hip_attention, 
    varlen_hip_attention, 
    paged_varlen_hip_attention,
    HiPAttentionArgs, 
    HiPAttentionOutputMetadata,
)

from vllm import _custom_ops as ops
from vllm.attention.backends.abstract import (
    AttentionBackend, 
    AttentionImpl,
    AttentionMetadata,
    AttentionMetadataBuilder,
    AttentionType
)
from vllm.attention.backends.utils import (
    PAD_SLOT_ID, 
    compute_slot_mapping,
    compute_slot_mapping_start_idx,
    is_block_tables_empty
)
from vllm.utils import async_tensor_h2d, make_tensor_with_pad

if TYPE_CHECKING:
    from vllm.worker.model_runner import ModelInputForGPUBuilder

class HiPAttentionEnvs:
    def __init__(self):
        self.show_warnings = os.getenv('HIP_WARNINGS', '0') == '1'
        
        self.refresh_interval = int(os.getenv('HIP_REFRESH_INTERVAL', '8'))
        self.hip_dense_layers = os.getenv('HIP_DENSE_LAYERS', '0,1,2')
        try:
            t = int(self.hip_dense_layers)
            warnings.warn(
                'You gave single integer for hip dense layers. '
                'From HiP 1.1, this changed into list of integers, e.g., `0,1,2` '
                'Are you sure about this?'
            )
            self.hip_dense_layers = list(range(t))
        except:
            self.hip_dense_layers = [int(i) for i in self.hip_dense_layers.split(',')]
        
        self.hip_k = int(os.getenv('HIP_K', '512'))
        self.hip_bq = int(os.getenv('HIP_BQ', '64'))
        self.hip_bsq = int(os.getenv('HIP_BSQ', '2'))
        self.hip_bk = int(os.getenv('HIP_BK', '2'))
        self.hip_bsk = int(os.getenv('HIP_BSK', '1'))
        self.hip_bk_after_mask = int(os.getenv('HIP_BK_AFTER_MASK', '-1'))
        
        self.hip_prefill_k = int(os.getenv('HIP_PREFILL_K', self.hip_k))
        self.hip_prefill_bq = int(os.getenv('HIP_PREFILL_BQ', self.hip_bq))
        self.hip_prefill_bsq = int(os.getenv('HIP_PREFILL_BSQ', self.hip_bsq))
        self.hip_prefill_bk = int(os.getenv('HIP_PREFILL_BK', self.hip_bk))
        self.hip_prefill_bsk = int(os.getenv('HIP_PREFILL_BSK', self.hip_bsk))
        self.hip_prefill_always_dense = os.getenv('HIP_PREFILL_ALWAYS_DENSE', '0') == '1'
        
        self.hip_sw = int(os.getenv('HIP_SW', '256'))
        self.hip_nsink = int(os.getenv('HIP_NSINK', '16'))
        
        self.hip_sample_method = os.getenv('HIP_SAMPLE_METHOD', 'center')
        
        self.hip_seq_threshold = int(os.getenv('HIP_SEQ_THRESH', '-1'))
        
        self.hip_offload = os.getenv('HIP_OFFLOAD', '0') == '1'
        
        print(f'Deocde Config: {self.decode_kwargs()}')
        print(f'Prefill Config: {self.prefill_kwargs()}')
    
    def decode_kwargs(self):
        return {
            'mask_k': self.hip_k,
            'block_size_q': self.hip_bq,
            'block_stride_q': self.hip_bsq,
            'block_size_k': self.hip_bk,
            'block_stride_k': self.hip_bsk,
            'block_size_k_after_masking': self.hip_bk_after_mask,
            'sample_method': self.hip_sample_method,
            'sliding_window_size': self.hip_sw,
            'sink_token_size': self.hip_nsink,
        }
    
    def prefill_kwargs(self):
        kwargs = copy.deepcopy(self.decode_kwargs())
        kwargs.update({
            'mask_k': self.hip_prefill_k,
            'block_size_q': self.hip_prefill_bq,
            'block_stride_q': self.hip_prefill_bsq,
            'block_size_k': self.hip_prefill_bk,
            'block_size_k_after_masking': self.hip_bk_after_mask,
            'block_stride_k': self.hip_prefill_bsk,
            'num_dense_queries': self.hip_seq_threshold,
        })
        return kwargs

envs = HiPAttentionEnvs()

class HiPAttentionBackend(AttentionBackend):

    @staticmethod
    def get_supported_head_sizes() -> List[int]:
        return [16, 32, 64, 128, 256, 512, 1024]

    @staticmethod
    def get_name() -> str:
        return "hip-attn"

    @staticmethod
    def get_impl_cls() -> Type["HiPAttentionImpl"]:
        return HiPAttentionImpl

    @staticmethod
    def get_metadata_cls() -> Type["AttentionMetadata"]:
        return HiPAttentionMetadata

    @staticmethod
    def get_builder_cls() -> Type["HiPAttentionMetadataBuilder"]:
        return HiPAttentionMetadataBuilder

    @staticmethod
    def get_kv_cache_shape(
        num_blocks: int,
        block_size: int,
        num_kv_heads: int,
        head_size: int,
    ) -> Tuple[int, ...]:
        if block_size % 16 != 0:
            raise ValueError("Block size must be a multiple of 16.")
        return (2, num_blocks, block_size, num_kv_heads, head_size)

    @staticmethod
    def swap_blocks(
        src_kv_cache: torch.Tensor,
        dst_kv_cache: torch.Tensor,
        src_to_dst: torch.Tensor,
    ) -> None:
        src_key_cache = src_kv_cache[0]
        dst_key_cache = dst_kv_cache[0]
        ops.swap_blocks(src_key_cache, dst_key_cache, src_to_dst)

        src_value_cache = src_kv_cache[1]
        dst_value_cache = dst_kv_cache[1]
        ops.swap_blocks(src_value_cache, dst_value_cache, src_to_dst)

    @staticmethod
    def copy_blocks(
        kv_caches: List[torch.Tensor],
        src_to_dists: torch.Tensor,
    ) -> None:
        key_caches = [kv_cache[0] for kv_cache in kv_caches]
        value_caches = [kv_cache[1] for kv_cache in kv_caches]
        ops.copy_blocks(key_caches, value_caches, src_to_dists)

@dataclass
class HiPAttentionMetadata(AttentionMetadata):
    """Metadata for HiPAttentionBackend.

    NOTE: Any python object stored here is not updated when it is
    cuda-graph replayed. If you have values that need to be changed
    dynamically, it should be stored in tensor. The tensor has to be
    updated from `CUDAGraphRunner.forward` API.
    """
    # (batch_size,). The sequence length per sequence. Sequence length means
    # the computed tokens + new tokens None if it is a decoding.
    seq_lens: Optional[List[int]]
    # seq_lens stored as a tensor.
    seq_lens_tensor: Optional[torch.Tensor]

    # NOTE(sang): Definition of context_len, query_len, and seq_len.
    # |---------- N-1 iteration --------|
    # |---------------- N iteration ---------------------|
    # |- tokenA -|......................|-- newTokens ---|
    # |---------- context_len ----------|
    # |-------------------- seq_len ---------------------|
    #                                   |-- query_len ---|

    # Maximum query length in the batch. None for decoding.
    max_query_len: Optional[int]
    # Maximum sequence length among prefill batch. 0 if there are decoding
    # requests only.
    max_prefill_seq_len: int
    # Maximum sequence length among decode batch. 0 if there are prefill
    # requests only.
    max_decode_seq_len: int
    # (batch_size + 1,). The cumulative subquery lengths of the sequences in
    # the batch, used to index into subquery. E.g., if the subquery length
    # is [4, 6], it is [0, 4, 10].
    query_start_loc: Optional[torch.Tensor]
    # (batch_size + 1,). The cumulative sequence lengths of the sequences in
    # the batch, used to index into sequence. E.g., if the sequence length is
    # [4, 6], it is [0, 4, 10].
    seq_start_loc: Optional[torch.Tensor]
    # (batch_size,) A tensor of context lengths (tokens that are computed
    # so far).
    context_lens_tensor: Optional[torch.Tensor]

    # (batch_size, max_blocks_per_seq).
    # Block addresses per sequence. (Seq id -> list of physical block)
    # E.g., [0, 1, 2] means tokens are stored in 0th, 1st, and 2nd blocks
    # in the kv cache. Each block can contain up to block_size tokens.
    # 2nd dimensions are padded up to max_blocks_per_seq if it is cuda-graph
    # captured.
    block_tables: Optional[torch.Tensor]

    # Whether or not if cuda graph is enabled.
    # Cuda-graph is currently enabled for decoding only.
    # TODO(woosuk): Move `use_cuda_graph` out since it's unrelated to attention.
    use_cuda_graph: bool

    _cached_prefill_metadata: Optional["HiPAttentionMetadata"] = None
    _cached_decode_metadata: Optional["HiPAttentionMetadata"] = None

    @property
    def prefill_metadata(self) -> Optional["HiPAttentionMetadata"]:
        if self.num_prefills == 0:
            return None

        if self._cached_prefill_metadata is not None:
            return self._cached_prefill_metadata

        assert self.seq_lens is not None
        assert self.seq_lens_tensor is not None
        assert self.query_start_loc is not None
        assert self.context_lens_tensor is not None
        assert self.block_tables is not None
        assert self.seq_start_loc is not None

        self._cached_prefill_metadata = HiPAttentionMetadata(
            num_prefills=self.num_prefills,
            num_prefill_tokens=self.num_prefill_tokens,
            num_decode_tokens=0,
            slot_mapping=self.slot_mapping[:self.num_prefill_tokens],
            seq_lens=self.seq_lens[:self.num_prefills],
            seq_lens_tensor=self.seq_lens_tensor[:self.num_prefills],
            max_query_len=self.max_query_len,
            max_prefill_seq_len=self.max_prefill_seq_len,
            max_decode_seq_len=0,
            query_start_loc=self.query_start_loc[:self.num_prefills + 1],
            seq_start_loc=self.seq_start_loc[:self.num_prefills + 1],
            context_lens_tensor=self.context_lens_tensor[:self.num_prefills],
            block_tables=self.block_tables[:self.num_prefills],
            use_cuda_graph=False,
        )
        return self._cached_prefill_metadata

    @property
    def decode_metadata(self) -> Optional["HiPAttentionMetadata"]:
        if self.num_decode_tokens == 0:
            return None

        if self._cached_decode_metadata is not None:
            return self._cached_decode_metadata
        assert self.block_tables is not None
        assert self.seq_lens_tensor is not None

        self._cached_decode_metadata = HiPAttentionMetadata(
            num_prefills=0,
            num_prefill_tokens=0,
            num_decode_tokens=self.num_decode_tokens,
            slot_mapping=self.slot_mapping[self.num_prefill_tokens:],
            seq_lens=None,
            seq_lens_tensor=self.seq_lens_tensor[self.num_prefills:],
            max_query_len=None,
            max_prefill_seq_len=0,
            max_decode_seq_len=self.max_decode_seq_len,
            query_start_loc=None,
            seq_start_loc=None,
            context_lens_tensor=None,
            block_tables=self.block_tables[self.num_prefills:],
            use_cuda_graph=self.use_cuda_graph,
        )
        return self._cached_decode_metadata


class HiPAttentionMetadataBuilder(AttentionMetadataBuilder[HiPAttentionMetadata]):

    def __init__(self, input_builder: "ModelInputForGPUBuilder"):
        self.slot_mapping: List[int] = []
        self.prefill_seq_lens: List[int] = []
        self.context_lens: List[int] = []
        self.block_tables: List[List[int]] = []
        self.curr_seq_lens: List[int] = []
        self.num_prefills = 0
        self.num_prefill_tokens = 0
        self.num_decode_tokens = 0
        self.has_prefix_cache_hit = False

        self.input_builder = input_builder
        self.runner = input_builder.runner
        self.sliding_window = input_builder.sliding_window
        self.block_size = input_builder.block_size
        self.use_v2_block_manager = (
            input_builder.scheduler_config.use_v2_block_manager)

    def _add_seq_group(
        self, 
        inter_data: "ModelInputForGPUBuilder.InterDataForSeqGroup",
        chunked_prefill_enabled: bool, 
        prefix_cache_hit: bool
    ):
        """Add a sequence group to the metadata. Specifically update/append
        1. context length.
        2. block table.
        3. slot mapping.
        """
        is_prompt = inter_data.is_prompt
        block_tables = inter_data.block_tables

        for (seq_id, token_len, seq_len, curr_seq_len, query_len, context_len,
             curr_sliding_window_block) in zip(
                 inter_data.seq_ids, [len(t) for t in inter_data.input_tokens],
                 inter_data.orig_seq_lens, inter_data.seq_lens,
                 inter_data.query_lens, inter_data.context_lens,
                 inter_data.curr_sliding_window_blocks):
            self.context_lens.append(context_len)

            if is_prompt:
                self.num_prefills += 1
                self.num_prefill_tokens += token_len
                self.prefill_seq_lens.append(seq_len)
            else:
                assert query_len == 1, (
                    "seq_len: {}, context_len: {}, query_len: {}".format(
                        seq_len, context_len, query_len))
                self.num_decode_tokens += query_len
                self.curr_seq_lens.append(curr_seq_len)

            # Compute block table.
            # TODO(sang): Combine chunked prefill and prefix caching by
            # only allowing multiple of block_size chunk size.
            # NOTE: This only works for oooooooxxx style attention.
            block_table = []
            if prefix_cache_hit:
                # NOTE(woosuk): For hip-attn, the block table should
                # include the entries for the incoming prefill tokens.
                block_table = block_tables[seq_id]
            elif ((chunked_prefill_enabled or not is_prompt)
                  and block_tables is not None):
                if curr_sliding_window_block == 0:
                    block_table = block_tables[seq_id]
                else:
                    block_table = block_tables[seq_id][
                        -curr_sliding_window_block:]
            self.block_tables.append(block_table)

            # Compute slot mapping.
            is_profile_run = is_block_tables_empty(block_tables)
            start_idx = compute_slot_mapping_start_idx(
                is_prompt, query_len, context_len, self.sliding_window,
                self.use_v2_block_manager)
            compute_slot_mapping(is_profile_run, self.slot_mapping, seq_id,
                                 seq_len, context_len, start_idx,
                                 self.block_size, inter_data.block_tables)

    def build(
        self, 
        seq_lens: List[int], 
        query_lens: List[int],
        cuda_graph_pad_size: int, 
        batch_size: int
    ):
        """Build attention metadata with on-device tensors.

        Args:
            seq_lens: The maybe padded sequence lengths of the input sequences.
            query_lens: The query lengths of the input sequences.
            cuda_graph_pad_size: The padding size for cuda graph.
                                 -1 if cuda graph is not used.
            batch_size: The maybe padded batch size.
        """
        prefix_cache_hit = any([
            inter_data.prefix_cache_hit
            for inter_data in self.input_builder.inter_data_list
        ])
        for inter_data in self.input_builder.inter_data_list:
            self._add_seq_group(inter_data,
                                self.input_builder.chunked_prefill_enabled,
                                prefix_cache_hit)

        device = self.runner.device
        use_captured_graph = cuda_graph_pad_size != -1

        max_query_len = max(query_lens)
        max_prefill_seq_len = max(self.prefill_seq_lens, default=0)
        max_decode_seq_len = max(self.curr_seq_lens, default=0)
        num_decode_tokens = self.num_decode_tokens

        if use_captured_graph:
            self.slot_mapping.extend([PAD_SLOT_ID] * cuda_graph_pad_size)
            self.block_tables.extend([] * cuda_graph_pad_size)
            num_decode_tokens = batch_size

            # The shape of graph_block_tables is
            # [max batch size, max context len // block size].
            input_block_tables = self.runner.graph_block_tables[:batch_size]
            for i, block_table in enumerate(self.block_tables):
                if block_table:
                    input_block_tables[i, :len(block_table)] = block_table
            block_tables = torch.from_numpy(input_block_tables).to(
                device=device, non_blocking=True)
        else:
            block_tables = make_tensor_with_pad(
                self.block_tables,
                pad=0,
                dtype=torch.int,
                device=device,
            )
        assert max_query_len > 0, ("query_lens: {}".format(query_lens))

        assert device is not None
        context_lens_tensor = async_tensor_h2d(self.context_lens, torch.int,
                                               device, self.runner.pin_memory)
        seq_lens_tensor = async_tensor_h2d(seq_lens, torch.int, device,
                                           self.runner.pin_memory)
        query_lens_tensor = async_tensor_h2d(query_lens, torch.long, device,
                                             self.runner.pin_memory)
        slot_mapping_tensor = async_tensor_h2d(self.slot_mapping, torch.long,
                                               device, self.runner.pin_memory)
        query_start_loc = torch.zeros(query_lens_tensor.shape[0] + 1,
                                      dtype=torch.int32,
                                      device=device)
        seq_start_loc = torch.zeros(seq_lens_tensor.shape[0] + 1,
                                    dtype=torch.int32,
                                    device=device)
        torch.cumsum(
            seq_lens_tensor,
            dim=0,
            dtype=seq_start_loc.dtype,
            out=seq_start_loc[1:]
        )
        torch.cumsum(
            query_lens_tensor,
            dim=0,
            dtype=query_start_loc.dtype,
            out=query_start_loc[1:]
        )

        return HiPAttentionMetadata(
            num_prefills=self.num_prefills,
            slot_mapping=slot_mapping_tensor,
            num_prefill_tokens=self.num_prefill_tokens,
            num_decode_tokens=num_decode_tokens,
            seq_lens=seq_lens,
            seq_lens_tensor=seq_lens_tensor,
            max_query_len=max_query_len,
            max_prefill_seq_len=max_prefill_seq_len,
            max_decode_seq_len=max_decode_seq_len,
            query_start_loc=query_start_loc,
            seq_start_loc=seq_start_loc,
            context_lens_tensor=context_lens_tensor,
            block_tables=block_tables,
            use_cuda_graph=use_captured_graph,
        )


class HiPAttentionImpl(AttentionImpl):
    """
    If the input tensors contain prompt tokens, the layout is as follows:
    |<--------------- num_prefill_tokens ----------------->|	
    |<--prefill_0-->|<--prefill_1-->|...|<--prefill_N-1--->|

    Otherwise, the layout is as follows:	
    |<----------------- num_decode_tokens ------------------>|	
    |<--decode_0-->|..........|<--decode_M-1-->|<--padding-->|

    Generation tokens can contain padding when cuda-graph is used.
    Currently, prompt tokens don't contain any padding.

    The prompts might have different lengths, while the generation tokens
    always have length 1.

    If chunked prefill is enabled, prefill tokens and decode tokens can be
    batched together in a flattened 1D query.

    |<----- num_prefill_tokens ---->|<------- num_decode_tokens --------->|
    |<-prefill_0->|...|<-prefill_N-1->|<--decode_0-->|...|<--decode_M-1-->|

    Currently, cuda graph is disabled for chunked prefill, meaning there's no
    padding between prefill and decode tokens.
    """

    def __init__(
        self,
        num_heads: int,
        head_size: int,
        scale: float,
        num_kv_heads: int,
        alibi_slopes: Optional[List[float]],
        sliding_window: Optional[int],
        kv_cache_dtype: str,
        blocksparse_params: Optional[Dict[str, Any]] = None,
        logits_soft_cap: Optional[float] = None,
        layer_index: int = 0,
    ) -> None:
        if blocksparse_params is not None:
            raise ValueError(
                "HiPAttention does not support block-sparse attention.")
        self.num_heads = num_heads
        self.head_size = head_size
        self.scale = float(scale)
        self.num_kv_heads = num_kv_heads
        if alibi_slopes is not None:
            alibi_slopes = torch.tensor(alibi_slopes, dtype=torch.float32)
        self.alibi_slopes = alibi_slopes
        self.sliding_window = ((sliding_window, sliding_window)
                               if sliding_window is not None else (-1, -1))
        self.kv_cache_dtype = kv_cache_dtype
        if logits_soft_cap is None:
            # In hip-attn, setting logits_soft_cap as 0 means no soft cap.
            logits_soft_cap = 0
        self.logits_soft_cap = logits_soft_cap

        assert self.num_heads % self.num_kv_heads == 0
        self.num_queries_per_kv = self.num_heads // self.num_kv_heads

        if sliding_window is not None:
            # NOTE(woosuk): hip-attn's sliding window does not work with
            # paged KV cache.
            raise ValueError("Sliding window is not supported in HiPAttention.")

        support_head_sizes = HiPAttentionBackend.get_supported_head_sizes()
        if head_size not in support_head_sizes:
            raise ValueError(
                f"Head size {head_size} is not supported by HiPAttention. "
                f"Supported head sizes are: {support_head_sizes}."
            )
        
        self.layer_index = layer_index
        self.envs = envs
        
        self.checkout_last_mask_metadata = False
        self.use_last_mask = False
        self.last_mask_metadata: Optional[
            HiPAttentionOutputMetadata] = None
        
        self.checkout_query = False
        self.last_query: Optional[torch.Tensor] = None
        self.use_query_prefix = False
        self.prefix_queries: Optional[torch.Tensor] = None
        self.prefix_query_alpha: Optional[torch.Tensor] = None
        
        self.force_dense = False

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: HiPAttentionMetadata,
        k_scale: float = 1.0,
        v_scale: float = 1.0,
        attn_type: AttentionType = AttentionType.DECODER,
    ) -> torch.Tensor:
        """Forward pass with HiPAttention.

        Args:
            query: shape = [num_tokens, num_heads * head_size]
            key: shape = [num_tokens, num_kv_heads * head_size]
            value: shape = [num_tokens, num_kv_heads * head_size]
            kv_cache = [2, num_blocks, block_size, num_kv_heads, head_size]
            attn_metadata: Metadata for attention.
        Returns:
            shape = [num_tokens, num_heads * head_size]
        """
        if attn_type != AttentionType.DECODER:
            raise NotImplementedError(
                "Encoder self-attention and "
                "encoder/decoder cross-attention "
                "are not implemented for "
                "HiPAttentionImpl"
            )

        # NOTE(woosuk): HiPAttention does not support FP8 KV cache.
        assert k_scale == 1.0 and v_scale == 1.0, (
            "key/v_scale is not supported in HiPAttention."
        )

        num_tokens, hidden_size = query.shape
        # Reshape the query, key, and value tensors.
        query = query.view(-1, self.num_heads, self.head_size)
        key = key.view(-1, self.num_kv_heads, self.head_size)
        value = value.view(-1, self.num_kv_heads, self.head_size)

        if kv_cache is not None:
            key_cache = kv_cache[0]
            value_cache = kv_cache[1]

            # Reshape the input keys and values and store them in the cache.
            # If kv_cache is not provided, the new key and value tensors are
            # not cached. This happens during the initial memory profiling run.
            ops.reshape_and_cache_flash(
                key,
                value,
                key_cache,
                value_cache,
                attn_metadata.slot_mapping.flatten(),
                self.kv_cache_dtype,
                k_scale,
                v_scale,
            )

        num_prefill_tokens = attn_metadata.num_prefill_tokens
        num_decode_tokens = attn_metadata.num_decode_tokens
        assert key.shape[0] == num_prefill_tokens + num_decode_tokens
        assert value.shape[0] == num_prefill_tokens + num_decode_tokens

        output = torch.empty_like(query)
        # Query for decode. KV is not needed because it is already cached.
        decode_query = query[num_prefill_tokens:]
        # QKV for prefill.
        query = query[:num_prefill_tokens]
        key = key[:num_prefill_tokens]
        value = value[:num_prefill_tokens]

        assert query.shape[0] == num_prefill_tokens
        assert decode_query.shape[0] == num_decode_tokens

        if prefill_meta := attn_metadata.prefill_metadata:
            # Prompt run.
            if (kv_cache is None or prefill_meta.block_tables is None
                    or prefill_meta.block_tables.numel() == 0):
                # normal attention
                # When block_tables are not filled, it means q and k are the
                # prompt, and they have the same length.
                
                assert self.alibi_slopes == None
                assert self.logits_soft_cap == 0
                assert self.sliding_window == (-1, -1)
                
                if  (prefill_meta.max_prefill_seq_len < envs.hip_seq_threshold) or\
                    (envs.hip_prefill_always_dense) or\
                    (self.layer_index in envs.hip_dense_layers):
                    out = flash_attn_varlen_func(
                        q=query,
                        k=key,
                        v=value,
                        cu_seqlens_q=prefill_meta.seq_start_loc,
                        cu_seqlens_k=prefill_meta.seq_start_loc,
                        max_seqlen_q=prefill_meta.max_prefill_seq_len,
                        max_seqlen_k=prefill_meta.max_prefill_seq_len,
                        softmax_scale=self.scale,
                        causal=True,
                        window_size=self.sliding_window,
                        alibi_slopes=self.alibi_slopes,
                        softcap=self.logits_soft_cap,
                    )
                else:
                    if envs.show_warnings:
                        warnings.warn('HiP is used in prefill')
                    out = varlen_hip_attention(
                        q=query,
                        softmax_scale=self.scale,
                        k=key,
                        v=value,
                        seq_lens=prefill_meta.seq_lens,
                        args=HiPAttentionArgs(**envs.prefill_kwargs())
                    )
                
                assert output[:num_prefill_tokens].shape == out.shape
                output[:num_prefill_tokens] = out
            else:
                # prefix-enabled attention
                
                assert prefill_meta.seq_lens is not None
                assert self.alibi_slopes is None
                assert self.logits_soft_cap == 0
                
                max_seq_len = max(prefill_meta.seq_lens)
                if  (max_seq_len < envs.hip_seq_threshold) or\
                    (envs.hip_prefill_always_dense) or\
                    self.layer_index in envs.hip_dense_layers:
                    output[:num_prefill_tokens] = flash_attn_varlen_func(
                        q=query,
                        k=key_cache,
                        v=value_cache,
                        cu_seqlens_q=prefill_meta.query_start_loc,
                        max_seqlen_q=prefill_meta.max_query_len,
                        cu_seqlens_k=prefill_meta.seq_start_loc,
                        max_seqlen_k=max_seq_len,
                        softmax_scale=self.scale,
                        causal=True,
                        alibi_slopes=self.alibi_slopes,
                        block_table=prefill_meta.block_tables,
                        softcap=self.logits_soft_cap,
                    )
                else:
                    if envs.show_warnings:
                        warnings.warn('HiP is used in prefix prefill')
                    output[:num_prefill_tokens] = paged_varlen_hip_attention(
                        q=query,
                        softmax_scale=self.scale,
                        seq_lens=prefill_meta.seq_lens,
                        args=HiPAttentionArgs(
                            k_cache=key_cache,
                            v_cache=value_cache,
                            block_table=prefill_meta.block_tables,
                            cache_seq_lens=prefill_meta.seq_lens_tensor,
                            **envs.prefill_kwargs(),
                        )
                    )

        if decode_meta := attn_metadata.decode_metadata:
            # Decoding run.
            
            query = decode_query.unsqueeze(1)
            if self.checkout_query:
                self.last_query = query
            
            assert self.alibi_slopes is None
            if (self.layer_index in envs.hip_dense_layers) or self.force_dense:
                context = flash_attn_with_kvcache(
                    query,
                    key_cache,
                    value_cache,
                    block_table=decode_meta.block_tables,
                    cache_seqlens=decode_meta.seq_lens_tensor,
                    softmax_scale=self.scale,
                    causal=True,
                    alibi_slopes=self.alibi_slopes,
                )
            else:
                if not self.use_last_mask:
                    self.last_mask_metadata = None
                
                if self.use_query_prefix:
                    assert self.prefix_query_alpha is not None
                    prefixed_query = torch.cat([self.prefix_queries, query], dim=1)
                    _, repeated_query = torch.broadcast_tensors(prefixed_query, query)
                    query = \
                        prefixed_query * self.prefix_query_alpha +\
                        repeated_query * (1 - self.prefix_query_alpha)
                
                if envs.show_warnings:
                        warnings.warn('HiP is used in decode')
                
                context, hip_meta = paged_hip_attention(
                    q=query,
                    softmax_scale=self.scale,
                    args=HiPAttentionArgs(
                        k_cache=key_cache,
                        v_cache=value_cache,
                        block_table=decode_meta.block_tables,
                        cache_seq_lens=decode_meta.seq_lens_tensor,
                        **envs.decode_kwargs(),
                    ),
                    previous_mask_metadata=self.last_mask_metadata,
                )
                
                if self.use_query_prefix:
                    context = context[:, self.prefix_queries.shape[1]:]
                
                if self.checkout_last_mask_metadata:
                    self.last_mask_metadata = hip_meta
                
            output[num_prefill_tokens:] = context.squeeze(1)

        # Reshape the output tensor.
        return output.view(num_tokens, hidden_size)
    