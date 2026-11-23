A language encoder reads a sentence and outputs a vector in the "meaning
space". Equipping this vector with a radius produces what resembles a sphere of
meanings. We can then check whether a given sentence falls within a given
sphere. This can be used to classify feeds by topic or detect unwanted feeds.

Instead of defining the vectors by numbers, we define them by sentences, encode
them into vectors at runtime, and average them to get a sphere center.

Finding the spheres for topic labels can be done by doing unsupervised
clustering on the meaning vectors of all sentences in our dataset, which can
later be labeled by hand. Many of the clusters will be neutral sentences that
should not be displayed as topics, but I think (untested) they are useful in
attracting sentences that would be otherwise classified incorrectly.

The advantage I see in this idea is that it seems easy to tune by hand, and it
is very cheap in training and inference. One can simply define a sphere by
hand.

```
- threshold: 0.42
  sentences:
  - Hurry up!
  - Don't lose this opportunity!
  - This is available for a limited time!
```

Model details: all-MiniLM-L6-v2, quantized ONNX

