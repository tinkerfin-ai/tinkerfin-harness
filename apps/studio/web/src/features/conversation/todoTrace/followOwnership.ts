interface FollowRecord {
  threadId: string
  includeTaskTrace: boolean
  controller: AbortController
  settled: Promise<void>
}

export interface FollowHandoffResult {
  epoch: number
  demotedThreadIds: string[]
}

/** 串行化唯一 true follow 与非当前 false follow 的所有权切换 */
export class TaskTraceFollowOwnership {
  private currentThreadId = ''
  private epoch = 0
  private generation = 0
  private records = new Map<string, FollowRecord>()
  private transition: Promise<void> = Promise.resolve()

  async handoff(nextThreadId: string): Promise<FollowHandoffResult> {
    const generation = this.generation
    return this.enqueue(async () => {
      if (generation !== this.generation) return { epoch: this.epoch, demotedThreadIds: [] }
      this.epoch += 1
      const demotedThreadIds: string[] = []
      const stopping = [...this.records.values()].filter((record) => {
        const demote = record.includeTaskTrace && record.threadId !== nextThreadId
        const promote = !record.includeTaskTrace && record.threadId === nextThreadId
        if (demote) demotedThreadIds.push(record.threadId)
        return demote || promote
      })
      for (const record of stopping) record.controller.abort()
      await Promise.allSettled(stopping.map((record) => record.settled))
      if (generation !== this.generation) return { epoch: this.epoch, demotedThreadIds: [] }
      this.currentThreadId = nextThreadId
      return { epoch: this.epoch, demotedThreadIds }
    })
  }

  async follow(
    threadId: string,
    operation: (options: {
      includeTaskTrace: boolean
      signal: AbortSignal
    }) => Promise<void>,
  ): Promise<void> {
    const generation = this.generation
    const record = await this.enqueue(async () => {
      if (generation !== this.generation) return null
      const includeTaskTrace = threadId === this.currentThreadId
      const existing = this.records.get(threadId)
      if (existing?.includeTaskTrace === includeTaskTrace && !existing.controller.signal.aborted) return existing
      if (existing) {
        existing.controller.abort()
        await Promise.allSettled([existing.settled])
      }
      if (generation !== this.generation) return null
      const controller = new AbortController()
      const owner: FollowRecord = {
        threadId,
        includeTaskTrace,
        controller,
        settled: Promise.resolve(),
      }
      owner.settled = operation({
        includeTaskTrace,
        signal: controller.signal,
      }).finally(() => {
        if (this.records.get(threadId) === owner) this.records.delete(threadId)
      })
      this.records.set(threadId, owner)
      return owner
    })
    await record?.settled
  }

  async stop(threadId: string): Promise<void> {
    const generation = this.generation
    await this.enqueue(async () => {
      if (generation !== this.generation) return
      const record = this.records.get(threadId)
      if (!record) return
      record.controller.abort()
      await Promise.allSettled([record.settled])
    })
  }

  abortAll() {
    this.epoch += 1
    this.generation += 1
    this.currentThreadId = ''
    for (const record of this.records.values()) record.controller.abort()
  }

  async close(): Promise<void> {
    this.abortAll()
    await this.enqueue(async () => {
      const records = [...this.records.values()]
      await Promise.allSettled(records.map((record) => record.settled))
      this.records.clear()
      this.currentThreadId = ''
    })
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.transition.then(operation, operation)
    this.transition = result.then(() => undefined, () => undefined)
    return result
  }
}
