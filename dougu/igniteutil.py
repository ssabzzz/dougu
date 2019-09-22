from collections import defaultdict

from .ignite import Engine, Events
from .ignite.handlers import EarlyStopping, ModelCheckpoint
from .ignite.contrib.handlers import CustomPeriodicEvent


def attach_lr_scheduler(
        evaluator, optim, conf, metric_name='acc', optimum='max'):
    if conf.learning_rate_scheduler == "plateau":
        from torch.optim.lr_scheduler import ReduceLROnPlateau
        lr_scheduler = ReduceLROnPlateau(
            optim, factor=0.5,
            patience=conf.learning_rate_scheduler_patience,
            mode=optimum,
            verbose=True)
    elif conf.learning_rate_scheduler:
        raise ValueError(
            "Unknown lr_scheduler: " + conf.learning_rate_scheduler)
    else:
        lr_scheduler = None

    if lr_scheduler is not None:
        @evaluator.on(Events.COMPLETED)
        def scheduler_step(evaluator):
            try:
                lr_scheduler.step(evaluator.state.metrics[metric_name])
            except Exception:
                import traceback
                traceback.print_exc()


def log_results(trainer, evaluator, eval_name):
    metrics = evaluator.state.metrics
    if not hasattr(trainer.state, 'last_scores'):
        trainer.state.last_scores = {}
    metrics_str = ' | '.join(
        f'{metric} {val:.4f}' for metric, val in metrics.items())
    trainer.log("epoch {:04d} {} {} | {}".format(
            trainer.state.epoch,
            eval_name,
            trainer.state.last_epoch_duration,
            metrics_str))
    for metric_name, metric_score in metrics.items():
        if metric_name == 'nll':
            continue
        attr_name = f'{eval_name}_{metric_name}'
        trainer.state.last_scores[attr_name] = metric_score
    if 'acc' in metrics:
        trainer.state.last_acc = metrics['acc']


def attach_result_log(
        trainer, evaluator, data_loaders, split_names=['train', 'dev'],
        eval_every=1):
    def _log_results(_trainer):
        for split_name in split_names:
            evaluator.run(data_loaders[split_name])
            log_results(_trainer, evaluator, split_name)

    if eval_every != 1:
        eval_events = CustomPeriodicEvent(n_epochs=eval_every)
        trainer.register_events(*eval_events.Events)
        eval_events.attach(trainer)
        eval_event = getattr(
            eval_events.Events, f'EPOCHS_{eval_every}_COMPLETED')
    else:
        eval_event = Events.EPOCH_COMPLETED
    trainer.add_event_handler(eval_event, _log_results)


def make_trainer(name='trainer'):
    def actual_decorator(update_func):
        def wrapper(*args, **kwargs):
            return Engine(update_func, name=name)
        return wrapper
    return actual_decorator


def make_evaluator(
        metrics, optim, conf, lr_metric='acc', optimum='max'):
    def actual_decorator(inference_func):
        def wrapper(*args, **kwargs):
            engine = Engine(inference_func)
            attach_lr_scheduler(
                engine, optim, conf, metric_name=lr_metric, optimum=optimum)

            @engine.on(Events.STARTED)
            def reset_io(engine):
                engine.state.io = defaultdict(list)

            for name, metric in metrics.items():
                metric.attach(engine, name)
            return engine
        return wrapper
    return actual_decorator


def make_engines(
        model, update, inference, *, rundir,
        checkpoint_metric='acc',
        checkpoint_metric_optimum='max'):
    trainer = update()
    evaluator = inference()
    if checkpoint_metric:
        sign = {
            'max': 1,
            'min': -1}[checkpoint_metric_optimum]
        checkpointer = ModelCheckpoint(
            rundir, 'final',
            score_name=checkpoint_metric,
            score_function=lambda _: sign * evaluator.state.metrics[
                checkpoint_metric],
            n_saved=1)
    trainer.add_event_handler(
        Events.COMPLETED, checkpointer, {'model': model})
    return trainer, evaluator