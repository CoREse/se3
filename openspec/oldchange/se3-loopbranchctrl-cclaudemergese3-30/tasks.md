# se3 loop应当在一个独立的branch上进行，当ctrl-c打断或者结束后再开启一个claude进程将其merge。如果se3 loop里调用了se3 collab，那collab的work branch也应该是在se3 loop的这个branch上分裂出来，然后合并到这个se3 loop上。如果这个功能已经实现了，则检查是否有bug或者实现不完全的地方。 (Iteration 30/30)

## Tasks

- [x] se3 loop应当在一个独立的branch上进行，当ctrl-c打断或者结束后再开启一个claude进程将其merge。如果se3 loop里调用了se3 collab，那collab的work branch也应该是在se3 loop的这个branch上分裂出来，然后合并到这个se3 loop上。如果这个功能已经实现了，则检查是否有bug或者实现不完全的地方。
