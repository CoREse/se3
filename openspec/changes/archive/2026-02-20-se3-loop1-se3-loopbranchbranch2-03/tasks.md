# 检查se3 loop：1. 是否是整个se3 loop都是在同一个branch上工作的，而非每一次迭代生成一个branch；2. 在loop结束后，或者被ctrl-c打断后，是否把branch merge回了se3 loop前的branch，并checkout回了原本的branch，并删除了se3 loop新增的branch；3. 在这个基础上，se3 loop --collab的branch是否也是正确工作了 (Iteration 3/5)

## Tasks

- [x] 检查se3 loop：1. 是否是整个se3 loop都是在同一个branch上工作的，而非每一次迭代生成一个branch；2. 在loop结束后，或者被ctrl-c打断后，是否把branch merge回了se3 loop前的branch，并checkout回了原本的branch，并删除了se3 loop新增的branch；3. 在这个基础上，se3 loop --collab的branch是否也是正确工作了
