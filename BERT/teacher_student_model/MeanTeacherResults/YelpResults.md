## Yelp

### settings
| max_seq_length | train_batch_size | eval_batch_size | teacher_train_epochs |student_train_epochs |top_k|
|:---------------|:-----------------|:----------------|:---------------------|:--------------------|:----|
|128             |4.0               |4.0              |10.0                  |3.0                  |100  |

### results

| No | alpha | beta | gamma |   TL     |    TU    | Student  |  Final    |
| -- | ----- | ---- | ----- | -------- | ---------| -------  | -------   |
|  1 |  0.1  |  0.1 |  0.8  | 0.414949 | 0.386464 | 0.427071 | 0.434646  |
|  2 |  0.1  |  0.2 |  0.7  |    -     |     -    |  -       | 0.434242  |
|  3 |  0.1  |  0.3 |  0.6  |    -     |     -    |  -       | 0.420000  |
|  4 |  0.1  |  0.4 |  0.5  |    -     |     -    |  -       | 0.401111  |
|  5 |  0.1  |  0.5 |  0.4  |    -     |     -    |  -       | 0.395959  |
|  6 |  0.1  |  0.6 |  0.3  |    -     |     -    |  -       | 0.395858  |
|  7 |  0.1  |  0.7 |  0.2  |    -     |     -    |  -       | 0.395960  |
|  8 |  0.1  |  0.8 |  0.1  |    -     |     -    |  -       | 0.391616  |
|  9 |  0.2  |  0.1 |  0.7  |    -     |     -    |  -       | 0.436161  |
|  10|  0.2  |  0.2 |  0.6  |    -     |     -    |  -       | 0.432121  |
|  11|  0.2  |  0.3 |  0.5  |    -     |     -    |  -       | 0.416465  |
|  12|  0.2  |  0.4 |  0.4  |    -     |     -    |  -       | 0.390707  |
|  13|  0.2  |  0.5 |  0.3  |    -     |     -    |  -       | 0.394141  |
|  14|  0.2  |  0.6 |  0.2  |    -     |     -    |  -       | 0.395353  |
|  15|  0.2  |  0.7 |  0.1  |    -     |     -    |  -       | 0.392323  |
|  16|  0.3  |  0.1 |  0.6  |    -     |     -    |  -       | 0.884646  |
|  17|  0.3  |  0.2 |  0.5  |    -     |     -    |  -       | 0.885151  |
|  18|  0.3  |  0.3 |  0.4  |    -     |     -    |  -       | 0.882828  |
|  19|  0.3  |  0.4 |  0.3  |    -     |     -    |  -       | 0.881010  |
|  20|  0.3  |  0.5 |  0.2  |    -     |     -    |  -       | 0.876868  |
|  21|  0.3  |  0.6 |  0.1  |    -     |     -    |  -       | 0.871818  |
|  22|  0.4  |  0.1 |  0.5  |    -     |     -    |  -       | 0.883939  |
|  23|  0.4  |  0.2 |  0.4  |    -     |     -    |  -       | 0.881010  |
|  24|  0.4  |  0.3 |  0.3  |    -     |     -    |  -       | 0.877272  |
|  25|  0.4  |  0.4 |  0.2  |    -     |     -    |  -       | 0.873030  |
|  26|  0.4  |  0.5 |  0.1  |    -     |     -    |  -       | 0.868383  |
|  27|  0.5  |  0.1 |  0.4  |    -     |     -    |  -       | 0.876465  |
|  28|  0.5  |  0.2 |  0.3  |    -     |     -    |  -       | 0.872121  |
|  29|  0.5  |  0.3 |  0.2  |    -     |     -    |  -       | 0.868888  |
|  30|  0.5  |  0.4 |  0.1  |    -     |     -    |  -       | 0.864343  |
|  31|  0.6  |  0.1 |  0.3  |    -     |     -    |  -       | 0.866970  |
|  32|  0.6  |  0.2 |  0.2  |    -     |     -    |  -       | 0.863535  |
|  33|  0.6  |  0.3 |  0.1  |    -     |     -    |  -       | 0.861616  |
|  34|  0.7  |  0.1 |  0.2  |    -     |     -    |  -       | 0.861414  |
|  35|  0.7  |  0.2 |  0.1  |    -     |     -    |  -       | 0.851919  |
|  36|  0.8  |  0.1 |  0.1  |    -     |     -    |  -       | 0.856970  |    