# 意图

探索在claude code下的Software Engineering 3.0范式。

阅读https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents这篇文章学习一下里面的做法，再结合Spec Driven Development（用openspec实现），探索怎么将它有机结合。同时，这个系统要在引入claude code的agent team的开发方式时也能原生支持。

人类的角色在于提供意图，做一些claude code暂时没有能力做的事等，这个系统应当将人类的调用也作为一种类似MCP的调用，并且由于人类调用并非一直available，所以要以最小的阻塞代价来执行人类调用（即人类调用不应当阻塞其它不相关任务的执行）。

探索一套完整自洽的开发体系，并在claude code上实现，探索应当怎样实现在claude code上（是skill？CLAUDE.md？还是什么？）