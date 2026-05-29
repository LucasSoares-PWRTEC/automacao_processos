import sys
sys.path.insert(0, '.')

from organizar_processos.flexao import _flex_judicial_treatment, Contexto

print('Testing _flex_judicial_treatment function:')
print('=' * 50)

# Test case 1: Masculine (should remain unchanged)
ctx_m = Contexto(genero_juiz='m')
texto = 'EXCELENTÍSSIMO SENHOR DOUTOR JUIZ'
result_m = _flex_judicial_treatment(texto, ctx_m)
expected_m = 'EXCELENTÍSSIMO SENHOR DOUTOR JUIZ'
print(f'Input:    {repr(texto)}')
print(f'Ctx gen:  {ctx_m.genero_juiz}')
print(f'Result:   {repr(result_m)}')
print(f'Expected: {repr(expected_m)}')
print(f'Match:    {result_m == expected_m}')
print()

# Test case 2: Feminine (should add 'A' markers)
ctx_f = Contexto(genero_juiz='f')
result_f = _flex_judicial_treatment(texto, ctx_f)
expected_f = 'EXCELENTÍSSIMA SENHORA DOUTORA JUIZA'
print(f'Input:    {repr(texto)}')
print(f'Ctx gen:  {ctx_f.genero_juiz}')
print(f'Result:   {repr(result_f)}')
print(f'Expected: {repr(expected_f)}')
print(f'Match:    {result_f == expected_f}')
print()