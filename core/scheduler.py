"""基于 APScheduler 的定时调度（支持随机扰动间隔）"""
import logging
import random
from datetime import time
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger


def run_scheduler(job, interval_minutes: int, run_on_start: bool = True,
                  jitter_minutes: int = 0, *, daily_times=None):
    """
    interval_minutes: 基础间隔(分钟)
    jitter_minutes:   每次执行额外的随机扰动(±jitter)，降低被风控识别为定时任务的概率
    """
    sched = BlockingScheduler(timezone="Asia/Shanghai")
    logger = logging.getLogger("jipiao")
    if daily_times:
        times = [time.fromisoformat(value) for value in daily_times]
        trigger = OrTrigger([CronTrigger(hour=value.hour, minute=value.minute,
                                         timezone=sched.timezone) for value in times])
        jitter_minutes = 0
    else:
        trigger = IntervalTrigger(minutes=interval_minutes)

    def wrapped():
        try:
            job()
        except Exception as e:
            logger.exception("任务执行失败: %s", e)
        # 下一次运行时间随机扰动
        if jitter_minutes > 0:
            try:
                base = sched.get_job("price_monitor")
                if base is not None:
                    from datetime import datetime, timedelta
                    next_delta = interval_minutes + random.uniform(-jitter_minutes, jitter_minutes)
                    next_delta = max(1, next_delta)  # 至少1分钟
                    next_time = datetime.now(sched.timezone) + timedelta(minutes=next_delta)
                    sched.modify_job("price_monitor", next_run_time=next_time)
                    logger.info("下次抓取计划: %s (间隔 %.1f 分钟)",
                                next_time.strftime("%H:%M:%S"), next_delta)
            except Exception as e:
                logger.warning("调整下次执行时间失败: %s", e)

    sched.add_job(
        wrapped,
        trigger=trigger,
        id="price_monitor",
        max_instances=1,
        coalesce=True,
    )
    if run_on_start:
        wrapped()
    sched.start()
