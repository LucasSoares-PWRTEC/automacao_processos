import re

# Test the regex patterns
RE_JUDICIAL_TREATMENT = re.compile(
    r"(EXCELENT[IÍ]SSIM)(A?)\s+(SENHOR)(A?)\s+(DOUTOR)(A?)\s+(JUIZ)(A?)",
    re.IGNORECASE,
)
RE_JUDICIAL_TREATMENT_MERIT = re.compile(
    r"(MERIT[IÍ]SSIM)(A?)\s+(SENHOR)(A?)\s+(DOUTOR)(A?)\s+(JUIZ)(A?)",
    re.IGNORECASE,
)

# Test cases
test_cases = [
    "EXCELENTÍSSIMO SENHOR DOUTOR JUIZ",
    "EXCELENTÍSSIMA SENHORA DOUTORA JUIZA",
    "MERITÍSSIMO SENHOR DOUTOR JUIZ",
    "MERITÍSSIMA SENHORA DOUTORA JUIZA",
    "EXCELENTÍSSIMO   SENHOR      DOUTOR    JUIZ",
]

for texto in test_cases:
    print(f"Texto: {repr(texto)}")
    match1 = RE_JUDICIAL_TREATMENT.search(texto)
    match2 = RE_JUDICIAL_TREATMENT_MERIT.search(texto)
    print(f"  Match EXCELENT: {bool(match1)}")
    print(f"  Match MERIT: {bool(match2)}")
    
    if match1:
        print(f"  Grupos EXCELENT: {match1.groups()}")
    if match2:
        print(f"  Grupos MERIT: {match2.groups()}")
    print()