# Plano de testes das otimizações

Cada otimização deste projeto afirma duas coisas ao mesmo tempo: que é **mais
rápida** e que **não muda o resultado**. As duas precisam ser verificadas juntas —
um ganho de performance que corrompe a árvore em silêncio não vale nada aqui, e
esta codebase já teve exatamente esse bug (diretórios-fantasma vindos de
`split('/')` ingênuo, ver `legacy.md`).

Este documento existe para que os números do capítulo de Avaliação da monografia
sejam **reproduzíveis**, e não apenas relatados.

## Ambiente

| Item | Valor |
|---|---|
| Python | 3.10.4 (pyenv, fixado por `.python-version`) |
| pandas / Flask | 2.2.3 / 3.1.3 |
| Node | v22.15.0 (só para o benchmark de frontend) |
| Dataset | `data/tokenFilesFull.csv` — kernel Linux, 3,5 MB, 66.943 nós (62.263 arquivos + 4.680 diretórios), profundidade máxima 11 |
| Payload gerado | 8,76 MB de JSON |

O `data/` é gitignored e não vem no repositório (ver `CLAUDE.md`). Sem ele os
scripts abortam com mensagem explícita.

> **Aviso sobre variância:** as medições abaixo foram tiradas com outros
> processos pesados na máquina. O mesmo comando em `main` deu 9,47s e 13,26s em
> execuções diferentes (~40% de variação). **Os tempos absolutos não são
> comparáveis entre sessões; as razões (speedup) são.** Para números de
> publicação, rode todas as variantes na mesma sessão, com a máquina ociosa, e
> reporte a razão junto do absoluto.

## Como rodar

```bash
# backend: compara refs git, mede frio/quente e prova identidade da saída
python bench/backend_bench.py main perf-lookup-vectorize

# gera o JSON da árvore para o benchmark de frontend
python bench/backend_bench.py --dump /tmp/tree.json perf-lookup-vectorize

# frontend: busca de nó por path (findInFull)
node bench/frontend_lookup_bench.mjs /tmp/tree.json
node bench/frontend_lookup_bench.mjs /tmp/tree.json --exhaustive   # ~2 min
```

`backend_bench.py` aceita qualquer ref git — extrai o `src/app.py` daquele ref
para um diretório temporário, aponta o `data/` real por symlink e importa o
módulo do zero a cada execução fria (para zerar o cache em memória).

---

## 1. Cache no backend — `eaa4b3f` (mergeado em `main`)

**Hipótese:** o CSV é relido e a árvore reconstruída a cada request, mesmo sem o
arquivo mudar. Cachear em memória, invalidado pelo `mtime`, elimina isso.

**Método:** `bench/backend_bench.py` mede duas requests no mesmo módulo — a
primeira (fria, constrói) e a segunda (quente, cache).

| Métrica | Resultado |
|---|---|
| Frio (constrói + serializa) | 13,26s |
| Quente (cache hit) | **1,26ms** |

**Achado do caminho:** cachear só o dict não bastava — o `jsonify()` sozinho
custava ~3s para serializar o payload a cada request. Cachear o `Response` já
serializado eliminou também esse custo.

**Limite conhecido:** o custo frio continua sendo pago uma vez por CSV. Com o
seletor de repositórios, isso passa a ser **uma vez por repositório escolhido**,
não por start do servidor — e o slot global único precisa virar cache por repo,
com evicção (o payload do kernel é 8,76 MB; repos maiores da amostra são
várias vezes isso).

## 2. Vetorização da construção da árvore — `fe98859` (`perf-lookup-vectorize`)

**Hipótese:** `df.iterrows()` mais loops Python dominam o tempo de build.
Substituir por operações vetorizadas do pandas reduz o custo frio.

**Método:** `bench/backend_bench.py main perf-lookup-vectorize`, 3 execuções
frias por ref, seguido da checagem de identidade e das invariantes.

| Ref | Frio (média) | Quente | Payload |
|---|---|---|---|
| `main` | 13,26s | 1,26ms | 8,76 MB |
| `perf-lookup-vectorize` | **1,39s** | 2,61ms | 8,76 MB |

**Speedup: 9,6× no frio** (10,5× numa medição com a máquina mais ociosa).

**Verificação de corretude — é a parte que importa:**

```
md5 main                  0b80868fed3f0a7383be37b86e15c625
md5 perf-lookup-vectorize 0b80868fed3f0a7383be37b86e15c625   → IDENTICAL
invariantes (ambos): nodes=66943 internal=4680 leaves=62263
                     internal_with_value=0  empty_children=0
```

Saída **byte-idêntica**, e as duas invariantes que o layout depende (nó interno
nunca carrega `value`, folha nunca carrega `children` vazio — ver `CLAUDE.md`)
valem nos dois. O autor da mudança também rodou um teste diferencial em 15 CSVs
sintéticos (colunas faltando, células NaN, pais órfãos, ids duplicados,
`node_name` contendo barra, `node_name` vazio, arquivo só com header, caminho
sem barra inicial) — todos batendo.

**Ideias rejeitadas por medição** (registrar na monografia: nem toda
"vetorização" é mais rápida):

| Ideia | Vetorizada | Alternativa escolhida |
|---|---|---|
| `str.rsplit('/', n=1, expand=True)` para o parent id | 0,158s | 0,067s (slice manual) |
| `to_dict('records')` | 0,472s | 0,087s (dict-comprehension com `zip`) |
| `groupby('parent_id')` | 0,152s | 0,061s (loop dict/append) |

O `.str` do pandas também é loop Python. E o `rsplit` **não é a mesma regra**:
re-deriva o segmento em vez de usar `node_name`, que é precisamente o bug de
diretórios-fantasma que a versão atual corrige.

## 3. Índice `path → nó` no lugar da varredura — `bca69b9` (`perf-lookup-vectorize`)

**Hipótese:** `findInFull` varria a hierarquia inteira a cada clique num tile —
e sem sair no primeiro match. Indexar uma vez torna a busca O(1).

**Método:** `bench/frontend_lookup_bench.mjs`, 200 alvos, metade deles além de
`MAX_DEPTH` (o caso que o `findInFull` existe para atender).

| Métrica | Resultado |
|---|---|
| Varredura linear | 3,69 ms/clique (66.943 nós visitados) |
| Lookup no Map | **0,000196 ms/clique** (1 hash) |
| Speedup | ~18.800× |
| Custo do índice | 48,7 ms, uma vez — amortiza em 14 cliques |

**Verificação de equivalência:** os paths são únicos (66.943 entradas para
66.943 nós), então a varredura antiga ("último match vence") e o Map devolvem
sempre o mesmo nó. O script checa isso em O(n) por padrão; `--exhaustive` roda o
cruzamento O(n²) contra a varredura real (~2 min, 0 divergências em todos os
66.943 paths).

## 4. Cache de layout no frontend — `frontend-layout-cache`

**Hipótese:** `render(focus)` recomputa `d3.hierarchy` + `sort` + `applyWeights`
+ `treemap()` do zero mesmo ao voltar para um nó já visitado (breadcrumb,
irmão já aberto, toggle linear/log já calculado). Um cache LRU por
`path + sizeMode` elimina o recálculo.

**Status:** implementação em andamento. Procedimento de medição a seguir:

1. Instrumentar com `performance.now()` em volta do bloco de layout em
   `render.js`, contando hits e misses.
2. Medir o custo de um layout frio na raiz e num diretório médio.
3. Medir o custo de um cache hit nos mesmos nós.
4. Percorrer: entrar em diretório → breadcrumb → entrar em irmão → alternar
   linear/log → busca que salta para arquivo. Conferir ausência de artefato
   visual (os bugs de tween obsoleto que o `CHANGELOG.md` já corrigiu são
   exatamente o que uma falha de cache reintroduz).
5. Comparar contra o custo de construção de DOM em `appendDepth`, que é o
   suspeito alternativo de gargalo — se o layout já for barato perto dele, o
   cache não se justifica.

**Critério de aceite:** o ganho precisa ser perceptível *e* o percurso acima
precisa ficar livre de artefatos. Se o layout não for o gargalo, registrar o
resultado negativo — é informação válida para a monografia.

---

## Otimizações anteriores a este plano

As otimizações de renderização do rework original (culling sub-pixel, renderização
progressiva por frame, animação adaptativa, labels via canvas) estão documentadas
em `CHANGELOG.md`, com os números relatados na época (~15.015 → ~2.795 tiles;
settle time ~500ms → ~25ms). Elas **não** foram medidas com este harness. Para o
capítulo de Avaliação, vale remedi-las nas mesmas condições das demais, para que
a comparação com a implementação anterior seja uniforme.

## Lacunas conhecidas deste plano

- **Nenhuma verificação de renderização no browser é automatizada.** Os
  benchmarks de frontend medem o algoritmo em Node; não provam que a página
  pinta corretamente. O percurso manual do item 4 continua necessário.
- **Um só dataset.** Todos os números vêm do kernel (66.943 nós). Os repositórios
  maiores da amostra (Firefox 419.572 nós, Waterfox 404.197, intellij-community
  316.683) têm ordem de grandeza diferente e mudariam as conclusões — sobretudo
  a de que o payload cabe confortavelmente no browser.
- **Não há teste de regressão.** Nada impede que uma mudança futura reintroduza
  os bugs que este projeto corrigiu. O `backend_bench.py` serve como teste de
  regressão de fato para o backend (checagem de invariantes + identidade de
  saída) e vale rodá-lo antes de cada merge.
