from __future__ import annotations


# Synchronized with ggml-org/llama.cpp include/llama.h `enum llama_ftype`
# as observed 2026-07-24. Removed historical slots stay readable because old
# GGUF files can still contain them. No llama.cpp runtime dependency is used.
LLAMA_FTYPE_LABELS: dict[int, str] = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    4: "Q4_1 + F16（旧）",
    5: "Q4_2（已移除）",
    6: "Q4_3（已移除）",
    7: "Q8_0",
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
    19: "IQ2_XXS",
    20: "IQ2_XS",
    21: "Q2_K_S",
    22: "IQ3_XS",
    23: "IQ3_XXS",
    24: "IQ1_S",
    25: "IQ4_NL",
    26: "IQ3_S",
    27: "IQ3_M",
    28: "IQ2_S",
    29: "IQ2_M",
    30: "IQ4_XS",
    31: "IQ1_M",
    32: "BF16",
    33: "Q4_0_4_4（已移除）",
    34: "Q4_0_4_8（已移除）",
    35: "Q4_0_8_8（已移除）",
    36: "TQ1_0",
    37: "TQ2_0",
    38: "MXFP4 MoE",
    39: "NVFP4",
    40: "Q1_0",
    41: "Q2_0",
}


def quantization_label(file_type: object) -> str:
    if isinstance(file_type, bool) or not isinstance(file_type, int):
        return ""
    return LLAMA_FTYPE_LABELS.get(
        file_type, f"未知（file_type={file_type}）"
    )
