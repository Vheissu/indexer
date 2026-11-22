# Machine learning in this project, for Python developers

**Purpose.** This article is for developers who know Python well but have never worked with machine
learning. It explains the ideas you need, and no more, to read, change and debug the three places
where this project uses models:

| what | where | kind of model |
| --- | --- | --- |
| a feed's languages | `src/indexer/text/language.py` | a pretrained classifier (fastText), used as is |
| a feed's topics | `src/indexer/text/topics.py`, `text/embeddings.py` | a pretrained embedding model (MiniLM), used as is |
| dropping likely-rejected survivors proposals | `src/indexer/survivors/training.py`, `scoring.py` | a small classifier **we train ourselves** |

You don't need to know any calculus or statistics beyond averages. Some familiarity with numpy
arrays (shapes, `@` for matrix products) helps. The rules and decisions behind these models
(thresholds, what was measured, what was rejected) are in CLAUDE.md; this file only teaches the
concepts.

## 1. What a model is

In ordinary code you write the rule: `if "casino" in title: reject()`. A **model** is a function
whose rule is *learned from examples* instead of written by hand. It has two phases:

- **Training**: show the model many examples with known answers ("this feed was accepted", "this
  one was rejected") and let an algorithm adjust the model's numbers (its **weights** or
  **parameters**) until its answers match the known ones as well as possible.
- **Inference** (or prediction): use the trained weights on new inputs. This is cheap: usually a
  few multiplications.

A model is therefore just *data* (the weights) plus a small fixed *function* that combines them
with the input. In `training.py` that data is a `ProposalModel`: a vocabulary, an `idf` vector and
a `weights` matrix, saved to `data/models/proposals-<hash>.npz`.

The kind of task here is **classification**: put each input into one of a few **classes**
(`accepted` / `rejected`, or `en` / `de` / ...). The known answers are called **labels**, and
learning from labelled examples is **supervised learning**.

## 2. Models need numbers: features and vectors

Models do arithmetic, so text must first become numbers. A list of numbers describing an input is
a **feature vector**; many of them stacked are a **feature matrix** (one row per input, one column
per feature). The project uses two ways to get there.

### Counting words (TF-IDF) — used by the proposal filter

1. **Tokenize**: split the text into **terms**. `training.tokens` lower-cases the text, takes the
   words, and adds pairs of neighbouring words (`press_release`), because a pair often means more
   than its two words apart.
2. **Vocabulary**: give each term seen in at least `MIN_DF` training examples a column number.
   Rarer terms are dropped: a term seen once can only teach the model to memorise one feed.
3. **TF** (term frequency): how often a term occurs in this text, damped with a log so that ten
   repetitions don't count ten times as much.
4. **IDF** (inverse document frequency): a weight per term, high for rare terms and low for terms
   found everywhere ("the", "posts"), because common terms don't help tell feeds apart.
5. Multiply TF by IDF, then scale each row to length 1, so a long text and a short one are compared
   on *what* they say, not *how much*.

The result is a wide, mostly-zero matrix: thousands of columns, a handful of non-zero cells per
row. Note that word order is lost (apart from the pairs): this is called a **bag of words**.

### Embeddings — used for topics

An **embedding** is a vector (here 384 numbers) produced by a pretrained neural network, chosen so
that *texts with similar meaning get nearby vectors*. "Async Rust without the pain" and "futures
and tokio tips" share no words but end up close together. You don't need to know how the network
works inside; treat it as a function `text -> vector`.

- The network is **all-MiniLM-L6-v2**, a small sentence-embedding model. We run it with
  **ONNX Runtime** (a fast, portable format for running trained networks, no PyTorch needed),
  pinned by checksum in the Dockerfile.
- Before the network, a **tokenizer** cuts the text into sub-word pieces and maps them to integer
  IDs. It must be the exact tokenizer the network was trained with, which is why it is pinned too.
  Texts are cut at `MAX_TOKENS`.
- **Cosine similarity** measures how close two vectors are: 1 means the same direction, 0 means
  unrelated. For vectors of length 1 it is simply the dot product, so comparing one vector with
  thousands at once is a single matrix product. That is why the stored embeddings are unit length.

## 3. Pretrained models: languages and topics

Neither of these is trained by us; we only run inference and add rules around the output.

**Languages** (`text/language.py`). fastText's `lid.176` is a classifier trained by Facebook AI
Research on 176 languages. For each post's text it returns its most likely language. A single
short title is easy to misread, so the code adds plain rules on top: texts shorter than
`MIN_TEXT_LEN` are skipped, and a language becomes a feed label only if it appears on at least
`MIN_ITEMS` posts and `MIN_SHARE` of the judged posts. This "model output + simple rules" pattern
is common and is where most tuning happens.

**Topics** (`text/topics.py`). This is classification *without training*, sometimes called
**zero-shot** classification:

1. Embed every topic name in `data/topics/topics.txt` once, ahead of time. The result is the
   **embedding set** `data/embeddings/topics.npy` (one row per name), rebuilt by
   `indexer embeddings` whenever the names, the model or the tokenizer change; the matching
   `.json` file records their checksums so a stale matrix is refused at load time.
2. For a feed, embed the text of its English posts (plus its title and subtitle) and average the
   vectors into one vector for the whole feed.
3. Compute the cosine similarity between that vector and every topic name, and keep the best ones
   that pass the thresholds (`MIN_SCORE`, `MARGIN`, `MIN_SPREAD`).

The thresholds decide the trade-off described in section 6: they were tuned by hand on a sample
of feeds, deliberately favouring correct topics over many topics.

## 4. Training our own model: the proposal filter

`survivors/training.py` learns from past reviews which proposed feeds a reviewer is likely to
reject. Read the file alongside this section; its comments follow the same steps.

**The data.** Each reviewed proposal file listed in `data/training.toml` gives labelled examples:
the text the reviewer saw (title, description, categories, recent posts) and the label (URL left
uncommented = accepted, commented out = rejected). Changing these files is changing the training
data, so it changes the model.

**The model: logistic regression.** Despite the name, it is a classifier, and one of the simplest:

1. A text's feature vector (TF-IDF row) is multiplied by a weight matrix with one column per class.
   Each term has a weight per class; a text's **raw score** for a class is the sum of its terms'
   weights (times their TF-IDF values), plus a **bias**, a constant per class.
2. **Softmax** turns the raw scores into probabilities that add up to 1. The score `scores()`
   returns is the probability of `accepted`.

Training means finding the weights. Learned weights are often readable: if reviewers kept rejecting
press-release feeds, a term like `press_release` ends up with a large weight towards `rejected`.

**How the weights are found: gradient descent.** Start with every weight at 0. Repeat `STEPS`
times:

1. Predict probabilities for all examples with the current weights.
2. Measure how wrong they are with a **loss function** (here *cross-entropy*: large when the model
   gives a low probability to the right answer).
3. Compute the **gradient**: for each weight, which direction to move it to lower the loss. For
   logistic regression this has a short formula, so no autodiff library is needed.
4. Move every weight a small step in that direction. The step size is the **learning rate**.

`_fit` uses **Adam**, a widely used variant of gradient descent that adapts the step size for each
weight. Because training starts from zeros and uses every example in each step (**full batch**),
nothing is random: the same files always produce the same model.

**Regularization.** With thousands of terms and a few hundred examples, the model could put a huge
weight on a term that happened to appear in three rejected feeds and fit the examples perfectly
while learning nothing general. This is **overfitting**: excellent on the training data, poor on
new data. The `L2` penalty adds the size of the weights to the loss, so large weights must earn
their place.

## 5. Measuring honestly: held-out data and cross-validation

A model always looks better on the examples it was trained on. To know how it will do on *new*
proposals you must score examples it has **not** seen:

- **Held-out set**: train on part of the data, test on the rest.
- **Cross-validation** (`FOLDS = 5`): split the examples into 5 groups; train on 4, score the 5th;
  repeat so each group is scored once by a model that never saw it. Every example gets an honest
  score while all data is still used.

`train_model` uses the cross-validated scores for one thing: the **threshold**. Survivors should
drop about `CUT` (30%) of new proposals, so the threshold is the 30% **quantile** of the honest
scores: the value below which 30% of them fall. Proposals scoring under it are dropped. The final
model is then trained on all examples.

## 6. Reading the numbers: precision, recall, AUC

A filter makes two kinds of mistakes: dropping a feed a reviewer would have accepted, and keeping
one they would have rejected. Moving the threshold trades one for the other. The usual words:

- **Precision**: of what the model picked, how much was right. (Topics: of the topics shown, how
  many fit the feed.)
- **Recall** (coverage): of everything it should have picked, how much it found. (Topics: how many
  feeds get a topic at all.)
- **AUC** (area under the ROC curve): one number for how well scores *rank* the classes,
  independent of any threshold. 0.5 is a coin toss, 1.0 is perfect. It equals the chance that a
  random accepted proposal scores higher than a random rejected one. The proposal filter measured
  about 0.81.

Two habits keep these numbers trustworthy: measure on data the model never saw (section 5), and
compare a change against the previous version on the same data.

## 7. Practical notes for this project

- **Where models come from.** MiniLM, its tokenizer and `lid.176` are downloaded at image build
  time, pinned by checksum in the Dockerfile, and found through environment variables
  (`INDEXER_MINILM_MODEL`, `INDEXER_MINILM_TOKENIZER`, `INDEXER_LID_MODEL`). Outside Docker, set
  those variables to local copies; the tests use fakes and need none of them.
- **Caching.** The proposal model's file name is a hash of the training config, every listed file
  and `training.py` itself. Any change to the data *or the code* (even a comment) gives a new name,
  so the next survivors run retrains once (up to a minute). Model files are never committed.
- **Stale embeddings.** Editing `topics.txt` makes `topics.npy` stale; loading refuses it until the
  embeddings workflow (or `indexer embeddings topics`) rebuilds it.
- **Text from feeds is untrusted.** Models only read it; nothing a model outputs is executed or
  fetched, and published text is escaped like everything else.
- **Changing a model or a threshold is a heuristic change.** As with every selection rule in this
  project, agree it first (see CLAUDE.md), measure it on held-out data, and record what was
  measured.
- **Try it.** `indexer languages` and `indexer topics` run the pretrained models on any text you
  type; `indexer train` trains the proposal filter and prints its size and threshold.

## Glossary

| term | meaning here |
| --- | --- |
| model | weights + a fixed function that turns an input into a prediction |
| training / inference | learning the weights / using them |
| class, label | a possible answer / the known answer of an example |
| feature vector | the numbers describing one input |
| TF-IDF | word counts weighted by how rare each word is |
| embedding | a vector from a pretrained network; similar meaning, nearby vectors |
| cosine similarity | closeness of two vectors, 1 = same direction |
| logistic regression | weighted sum of features per class, then softmax |
| softmax | turns raw scores into probabilities summing to 1 |
| loss | how wrong the predictions are; training lowers it |
| gradient descent, Adam | repeatedly nudging weights to lower the loss |
| learning rate | the size of each nudge |
| overfitting | fitting the training examples instead of the pattern |
| regularization (L2) | penalising large weights to prevent overfitting |
| cross-validation | scoring each example with a model that didn't see it |
| threshold, quantile | the score cut-off / the value below which a share of scores fall |
| precision, recall, AUC | how right the picks are / how many are found / ranking quality |
| zero-shot | classifying into classes the model was never trained on (topic names) |
| ONNX, tokenizer | a portable format for running networks / text to integer pieces |
