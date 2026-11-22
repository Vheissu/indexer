# Machine learning basics

**Purpose.** This article gives Python developers with no machine learning background the
prerequisite knowledge for working on this project: what a model is, how text becomes numbers,
how a model is trained and how to tell whether it is any good. It teaches the general ideas, not
this project's code; examples from the project appear only where they make an idea easier to
grasp. No calculus or statistics beyond averages is assumed. Some familiarity with numpy arrays
(shapes, `@` for matrix products) helps.

## 1. What a model is

In ordinary code you write the rule yourself: `if "casino" in title: reject()`. A **model** is a
function whose rule is *learned from examples* instead. It has two parts:

- **Parameters** (or **weights**): numbers, often thousands or millions of them.
- A small fixed **function** that combines the parameters with an input to produce an output.

Working with a model has two phases:

- **Training**: show it many examples with known answers and let an algorithm adjust the weights
  until the model's answers match the known ones as well as possible. This is the slow part.
- **Inference** (or prediction): apply the trained weights to new inputs. Usually fast: a few
  multiplications and additions.

So a trained model is mostly *data*. Saving a model means saving its weights to a file; loading
it means reading them back.

## 2. Kinds of problems

- **Classification**: put each input into one of a fixed set of **classes**. Is this feed one a
  reviewer would accept or reject? Which language is this text in?
- **Regression** (not to be confused with logistic regression): predict a number (a price, a
  duration). Not used here, but the word turns up.
- **Supervised learning**: learning from examples whose correct answers, called **labels**, are
  known. Most practical classification is supervised.
- **Pretrained models**: models someone else trained, often on far more data than you have, that
  you download and only run. You can use them as they are or build on their output.

## 3. Turning inputs into numbers

Models do arithmetic, so every input must first become a list of numbers, a **feature vector**.
Many vectors stacked as rows form a **feature matrix**: one row per input, one column per
**feature**. Choosing and computing features is often most of the work.

For text there are two broad approaches.

### Counting words

1. **Tokenize**: split the text into units, called **tokens** or **terms**: usually lower-cased
   words, sometimes also pairs of neighbouring words (*bigrams*), because "press release" says more
   than "press" and "release" apart.
2. **Vocabulary**: give every term seen in the training data a column number. Very rare terms are
   usually dropped; a word seen once can't teach anything general.
3. **Count**: a text's vector holds, for each vocabulary term, how often the text uses it.

Word order is lost, which is why this is called a **bag of words**. Raw counts have two problems:
common words ("the", "and") dominate, and long texts get bigger numbers than short ones.
**TF-IDF** fixes both:

- **TF** (term frequency): the count, often damped with a logarithm, so ten repetitions don't
  weigh ten times as much as one.
- **IDF** (inverse document frequency): a weight per term, high for terms that appear in few
  texts and low for terms that appear in almost all, because rare terms tell texts apart better.
- Multiply the two, then scale each row to length 1 (**normalize**), so texts are compared on what
  they say rather than how long they are.

The result is a wide, *sparse* matrix: thousands of columns, nearly all zero in any one row.

### Embeddings

An **embedding** is a vector, typically a few hundred numbers, produced by a pretrained neural
network so that *texts with similar meaning get nearby vectors*, even when they share no words.
"Async Rust without the pain" and "tips for futures and tokio" end up close together; a recipe
blog ends up far away. You don't need to know how the network works inside; treat it as a
function from text to vector.

Two related terms:

- A **tokenizer** turns text into the integer IDs a network expects, usually by cutting it into
  sub-word pieces ("unbelievable" might become "un", "believ", "able"). A network only works with
  the exact tokenizer it was trained with, and it reads at most a fixed number of tokens.
- **ONNX** is a file format for trained networks that a small runtime can execute from Python,
  without installing a full deep-learning framework like PyTorch.

### Measuring similarity

To compare two vectors, use **cosine similarity**: 1 when they point the same way, about 0 when
unrelated. For vectors already scaled to length 1 it is just the dot product, so comparing one
vector with thousands at once is a single matrix product:

```python
import numpy as np
from numpy.typing import NDArray

names: NDArray[np.float64] = np.array([[0.6, 0.8], [1.0, 0.0]])  # one unit-length row each
query: NDArray[np.float64] = np.array([0.8, 0.6])  # unit length too
similarity: NDArray[np.float64] = names @ query
print(similarity)  # [0.96 0.8 ]: the first row is closer
```

Similarity alone can classify without any training: embed the name of each class ("rust",
"cooking", "photography"), embed the input, and pick the closest names. This is called
**zero-shot** classification, and it is how a feed can be given topics that no model was trained
on.

## 4. A simple trainable classifier: logistic regression

Despite its name, **logistic regression** is a classifier, and one of the simplest worth knowing.
For each class it has one weight per feature plus a constant, the **bias**:

1. **Raw scores**: for each class, multiply each feature by its weight and add them up, plus the
   bias. With a feature matrix `X` (one row per input) and a weight matrix `W` (one column per
   class) that is `X @ W`.
2. **Softmax** turns each row of raw scores into probabilities that are positive and sum to 1:
   exponentiate each score, then divide by the row's total.

```python
import numpy as np
from numpy.typing import NDArray

Matrix = NDArray[np.float64]


def softmax(z: Matrix) -> Matrix:
    e = np.exp(z - z.max(axis=1, keepdims=True))  # subtracting the max avoids overflow
    return e / e.sum(axis=1, keepdims=True)


# raw scores 2 and 0 as probabilities:
print(softmax(np.array([[2.0, 0.0]])).round(2))  # [[0.88 0.12]]
```

Its weights are easy to read: a feature with a large positive weight for a class pushes inputs
towards that class. If reviewers often rejected press-release feeds, the term "press release"
would end up with a large weight towards *rejected*.

## 5. How training works

Training means finding weights that make the model's predictions match the labels. The standard
method is **gradient descent**:

1. Start with some weights, often all zeros.
2. Predict for the training examples with the current weights.
3. Compute the **loss**: one number for how wrong the predictions are. For classification it is
   usually **cross-entropy**, which is large when the model gives a low probability to the right
   answer.
4. Compute the **gradient**: for every weight, which direction (and how strongly) to change it to
   lower the loss. Libraries compute it automatically; for logistic regression it has a short
   formula: `X.T @ (predicted - actual) / len(X)`.
5. Move every weight a small step against the gradient. The step size is the **learning rate**:
   too large and training jumps around, too small and it takes forever.
6. Repeat for a fixed number of steps, or until the loss stops improving.

Common variations you will meet:

- **Full batch** versus **mini-batch**: use all examples in every step, or a random handful.
  Full batch is simpler and deterministic, and fine when the data fits in memory.
- **Adam**: a popular version of gradient descent that adapts the step size of every weight
  separately, keeping running averages of each gradient and of its size. It usually needs less
  tuning than a fixed learning rate.
- **Determinism**: with no randomness (zero start, full batch, fixed number of steps), the same
  data always gives the same model, which makes results reproducible and easy to cache.

## 6. Overfitting and regularization

A model with many weights and few examples can **memorise** them instead of learning the
pattern. Say a word appears in three rejected examples by coincidence: the model can give it a
huge weight, fit the training data perfectly, and be wrong on new inputs. This is
**overfitting**: excellent on the training data, poor on anything else.

Two standard defences:

- **Ignore rare features**, such as terms seen in only one or two examples.
- **Regularization**: add a penalty for large weights to the loss. The most common is **L2**, the
  sum of the squared weights times a small factor. A weight then has to earn its size by
  explaining many examples.

## 7. Measuring honestly

A model always looks better on the examples it learned from, so **never judge a model on its
training data**. Score it on examples it has not seen:

- **Held-out (test) set**: set some labelled examples aside, train on the rest, and measure on the
  ones set aside.
- **Cross-validation**: split the examples into *k* groups (*folds*), commonly 5. Train on *k − 1*
  of them and score the remaining one; repeat until every group has been scored once. Every
  example gets an honest score from a model that never saw it, and no data is wasted. Then train
  the final model on all the data.

## 8. Thresholds and metrics

A classifier usually outputs a probability, and you turn it into a decision with a **threshold**:
drop an item if its probability of being acceptable is below *t*. The threshold is a choice, not
something the model learns. One practical way to pick it is by **quantile**: if you want to drop
the lowest-scoring 30% of items, set *t* to the score below which 30% of the honest
(cross-validated) scores fall.

Every threshold trades two kinds of mistakes: dropping good items and keeping bad ones. The
metrics that describe the trade:

- **Precision**: of the items the model picked, the share that was right. For topics: of the
  topics shown for a feed, how many fit.
- **Recall**: of the items it should have picked, the share it found. For topics: how many feeds
  get a topic at all.
- Raising a threshold usually raises precision and lowers recall, and the other way round.
  Deciding which matters more is a product decision, not a technical one.
- **AUC** (area under the ROC curve) measures how well the scores *rank* positives above
  negatives, whatever the threshold: the probability that a random positive example scores higher
  than a random negative one. 0.5 is a coin toss and 1.0 is perfect; around 0.8 is useful for a
  filter that saves reviewers work.

Two habits keep these numbers meaningful: always measure on unseen data, and compare a change
against the old version on the *same* data.

## 9. Habits that carry over from ordinary programming

- **Pin models like dependencies.** A pretrained model file, its tokenizer, and anything computed
  from them (like stored embeddings) belong together; a new model version silently changes every
  output. Pin exact versions and checksums, and recompute derived data when they change.
- **Treat a model's output as a guess.** Wrap it in plain rules where mistakes are costly. For
  example, label a feed with a language only when several of its posts agree, not on the strength
  of one short title.
- **Retraining is a code change.** New training data or a new threshold changes behaviour as much
  as editing a function; measure it before accepting it.
- **Inputs can be hostile.** A model reading untrusted text is still reading untrusted text: never
  execute or fetch anything because a model said so, and escape anything you publish.

## 10. Examples: the models this project uses

Short sketches of the three kinds of model, stripped down to the idea; the real code adds error
handling and rules around them. The comments under each `print` show its output. For the two
pretrained models the numbers are illustrative (they depend on the model file); the trained
classifier's are real, from running the snippet.

### Language identification: a pretrained classifier

fastText's `lid.176` model was trained on text in 176 languages. Inside, it turns a text into a
vector by averaging the vectors of its words and of their **character n-grams** (short letter
sequences such as `sch`, `ung`, `ein`). In this context **the vector describes how the text is
spelled**: which letter patterns it uses, and spelling is what gives a language away. A linear
classifier like the one in section 4 then scores that vector against each of the 176 languages,
and softmax turns the scores into probabilities. You never see the vector; you get the classes:

```python
import fasttext

model = fasttext.load_model("lid.176.ftz")

labels: tuple[str, ...]
labels, probs = model.predict("Wie man einen Sauerteig ansetzt und pflegt", k=3)  # top 3
for label, prob in zip(labels, probs):
    print(label.removeprefix("__label__"), round(float(prob), 2))
# de 0.97
# nl 0.01
# en 0.01
```

A single short title is easy to misread, so plain rules sit on top: skip texts too short to
judge, take each post's most likely language, and label the feed only with languages that enough
posts agree on.

```python
from collections import Counter

posts: list[str] = [
    "Wie man einen Sauerteig ansetzt und pflegt",
    "Brot backen im Winter: Tipps für die Gare",
    "OK",  # too short: not judged
]
votes: Counter[str] = Counter(
    model.predict(p)[0][0].removeprefix("__label__") for p in posts if len(p) >= 40
)
feed_languages: list[str] = [lang for lang, n in votes.items() if n >= 2]
print(votes, feed_languages)  # Counter({'de': 2}) ['de']
```

### Embeddings and zero-shot topics: a pretrained network

MiniLM turns a text into 384 numbers. In this context **the vector is the text's position in a
"meaning space"**: the network was trained so that texts about the same subject land near each
other, whatever words they use. No single number means anything on its own ("dimension 17" is not
"programming"); only distances between vectors do. Two consequences follow:

- The average of a feed's post vectors is the **centre of what the feed writes about**.
- A topic name like "rust" also has a position, so "how much is this feed about rust?" becomes
  "how close is the feed's centre to the word *rust*?": cosine similarity.

The tokenizer produces the network's input; the network returns one vector per token, which are
averaged into one vector per text and scaled to length 1:

```python
import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray
from tokenizers import Tokenizer

Vectors = NDArray[np.float32]  # one row per text

tokenizer = Tokenizer.from_file("tokenizer.json")
tokenizer.enable_padding()  # pad a batch to one length; the attention mask marks real tokens
session = ort.InferenceSession("minilm.onnx")


def embed(texts: list[str]) -> Vectors:
    encoded = tokenizer.encode_batch(texts)
    ids: NDArray[np.int64] = np.array([e.ids for e in encoded])
    mask: NDArray[np.int64] = np.array([e.attention_mask for e in encoded])
    inputs = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
    tokens: Vectors = session.run(None, inputs)[0]  # shape (texts, tokens, 384)
    vectors = (tokens * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)  # unit length


v = embed(["Async Rust without the pain"])
print(v.shape, v[0, :4].round(2))  # (1, 384) [-0.07  0.02 -0.04  0.05]: meaningless one by one
```

Topics without training: embed the topic names once, embed the feed's posts, average them, and
keep the names closest to the feed's centre:

```python
names: list[str] = ["rust", "cooking", "photography"]
name_vectors: Vectors = embed(names)  # computed once and stored

posts = embed(["Async Rust without the pain", "Lifetimes explained with examples"])
feed: Vectors = posts.mean(axis=0)
feed /= np.linalg.norm(feed)  # back to unit length, so the dot product is the cosine
similarity: Vectors = name_vectors @ feed
for name, score in zip(names, similarity):
    print(f"{name:12} {score:.2f}")
# rust         0.52
# cooking      0.05
# photography  0.03
topics: list[str] = [n for n, score in zip(names, similarity) if score >= 0.35]
print(topics)  # ['rust']
```

### A classifier we train: TF-IDF and logistic regression

Labelled examples in, weights out. Four toy reviews here; a real filter learns from hundreds.

In this context **each text's vector is a row of word weights**: one column per vocabulary word,
holding how much that word stands out in this text (TF-IDF), zero for words the text doesn't use.
Unlike an embedding, every number has a meaning you can read. Training then gives every word a
weight per class, so the model is readable too: the words that push a text towards *rejected*.

```python
import numpy as np
from numpy.typing import NDArray

Matrix = NDArray[np.float64]
# softmax(z: Matrix) -> Matrix as defined in section 4

texts: list[str] = [
    "indie game dev diary",
    "press release new product launch",
    "notes on rust compilers",
    "press release award winning service",
]
y: NDArray[np.int64] = np.array([0, 1, 0, 1])  # what the reviewer decided: 0 accepted, 1 rejected

# Features: one column per word of the vocabulary.
vocab: list[str] = sorted({w for t in texts for w in t.split()})
counts: Matrix = np.array([[t.split().count(w) for w in vocab] for t in texts], dtype=float)
idf: Matrix = np.log((1 + len(texts)) / (1 + (counts > 0).sum(axis=0))) + 1
X: Matrix = counts * idf
X /= np.linalg.norm(X, axis=1, keepdims=True)
print(len(vocab), "words:", vocab[:6], "...")
# 16 words: ['award', 'compilers', 'dev', 'diary', 'game', 'indie'] ...
print("vector of text 1:", {w: round(float(v), 2) for w, v in zip(vocab, X[1]) if v})
# vector of text 1: {'launch': 0.49, 'new': 0.49, 'press': 0.38, 'product': 0.49, 'release': 0.38}
X = np.hstack([X, np.ones((len(X), 1))])  # a column of ones: the bias

# Training: gradient descent on cross-entropy, with an L2 penalty.
W: Matrix = np.zeros((X.shape[1], 2))  # one weight per word (+ bias) per class
target: Matrix = np.eye(2)[y]  # one-hot labels: [1, 0] or [0, 1]
for _ in range(400):
    probs: Matrix = softmax(X @ W)
    W -= 0.5 * (X.T @ (probs - target) / len(X) + 1e-3 * W)

for word in ("press", "rust", "diary"):
    print(f"weight of {word!r} towards rejected: {W[vocab.index(word), 1]:+.2f}")
# weight of 'press' towards rejected: +1.41
# weight of 'rust' towards rejected: -0.97
# weight of 'diary' towards rejected: -0.97

# Inference: a new text goes through the same steps (unknown words are ignored).
def p_accepted(text: str) -> float:
    row: Matrix = np.array([[text.split().count(w) for w in vocab]], dtype=float) * idf
    row /= np.linalg.norm(row, axis=1, keepdims=True)
    return float(softmax(np.hstack([row, [[1.0]]]) @ W)[0, 0])


for new in ("press release new product", "press release on our rust compiler"):
    p = p_accepted(new)
    print(f"P(accepted | {new!r}) = {p:.2f} -> {'keep' if p >= 0.5 else 'drop'}")
# P(accepted | 'press release new product') = 0.02 -> drop
# P(accepted | 'press release on our rust compiler') = 0.52 -> keep
```

The new texts show both the strength and the limit of a bag of words. "press release new product" is
dropped: every word is one the model learned to distrust. "press release on our rust compiler" is a
coin toss: "press" and "release" pull towards *rejected*, "rust" and "on" (both from accepted texts)
pull the other way, "our" is unknown, and so is "compiler" (the vocabulary only has "compilers"), so
both count for nothing. More examples and word pairs fix much of this; that is why real filters train
on hundreds of reviews.

## Glossary

| term | meaning |
| --- | --- |
| model, parameters, weights | a function whose numbers (weights) are learned from examples |
| training, inference | learning the weights; using them on new inputs |
| class, label | a possible answer; the known answer of an example |
| supervised learning | learning from labelled examples |
| pretrained model | a model trained by someone else, used as is |
| feature, feature vector, matrix | a number describing an input; all of them for one input; one row per input |
| token, vocabulary | a unit of text (word, word pair, sub-word); the terms that get a column |
| bag of words, TF-IDF | word counts, ignoring order; counts weighted by each word's rarity |
| embedding | a vector from a pretrained network: similar meaning, nearby vectors |
| cosine similarity | how closely two vectors point the same way (1 = identical) |
| zero-shot | classifying by similarity to class names, with no training for those classes |
| logistic regression | weighted sum of features per class, then softmax |
| softmax | turns raw scores into probabilities that sum to 1 |
| loss, cross-entropy | how wrong predictions are; the usual loss for classification |
| gradient descent, learning rate, Adam | nudging weights to lower the loss; the nudge size; an adaptive variant |
| overfitting | fitting the training examples instead of the pattern |
| regularization, L2 | penalising large weights to reduce overfitting |
| held-out set, cross-validation | unseen examples for testing; scoring every example with a model that didn't see it |
| threshold, quantile | the score cut-off for a decision; the value below which a given share of scores fall |
| precision, recall, AUC | how right the picks are; how many were found; how well scores rank |
| tokenizer, ONNX | text to integer IDs for a network; a portable format for running networks |
