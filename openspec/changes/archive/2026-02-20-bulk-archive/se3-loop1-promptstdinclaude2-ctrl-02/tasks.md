# 对se3 loop进行两项修改：1. 不要用临时文件，而是将prompt用stdin或者参数的形式传给claude，让用户能看到说了什么。2. 监听ctrl-c，按一次进入补充说明状态，用户可以补充prompt，在当前loop就像正常加入了一句话，在之后的loop中也加入到初始prompt中，如果输入了空字符串则无事发生继续工作，在补充说明状态再按一次ctrl-c则退出。 (Iteration 2/10)

## Tasks

- [ ] 对se3 loop进行两项修改：1. 不要用临时文件，而是将prompt用stdin或者参数的形式传给claude，让用户能看到说了什么。2. 监听ctrl-c，按一次进入补充说明状态，用户可以补充prompt，在当前loop就像正常加入了一句话，在之后的loop中也加入到初始prompt中，如果输入了空字符串则无事发生继续工作，在补充说明状态再按一次ctrl-c则退出。
