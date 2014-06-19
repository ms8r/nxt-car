import time
import threading
import Queue
import random
import logging
from nxt.locator import find_one_brick, BrickNotFoundError
from nxt.brick import Brick
import nxt.sensor as ns
import nxt.motor as nm

logging.basicConfig(level=logging.DEBUG,
        format='%(threadName)s:%(levelname)s:%(message)s')

_stop_token = object()

_usb_sleep_multiplier = 0.2

class ExternalInterface(threading.Thread):
    def __init__(self, externalCallable, **kwds):
        threading.Thread.__init__(self, **kwds)
        self.setDaemon(1)
        self.externalCallable = externalCallable
        self.workRequestQueue = Queue.Queue( )
        self.resultQueue = Queue.Queue( )
        self.start( )
    def request(self, *args, **kwds):
        "called by other threads as externalCallable would be"
        self.workRequestQueue.put((args,kwds))
        return self.resultQueue.get( )
    def run(self):
        while 1:
            args, kwds = self.workRequestQueue.get( )
            self.resultQueue.put(self.externalCallable(*args, **kwds))


class MotorTouchThread(ExternalInterface, ns.Touch):
    def __init__(self, brick, port, stopQueue, resultQueue, **kwds):
        """
        Implememts touch sensor in its own thread. Directly communicates with
        associated motor thread via resultQueue.

        Arguments:
        ----------
        brick : nxt.brick.Brick object
        port : int
            Port number to which sensor is connected
        stopQueue : Queue object
            run methos will poll this queue for _stop_token, ignoring
            everyting else. _stop_token will be put back onto queue before
            breaking.
        resultQueue : Queue object
            Request queue of associated motor; will put 'action' keyword with
            'start' or 'stop' value on queue if button achtion is detected.

        **kwds will be passed to Thread constructor and should include *name*
        keyword to make debugging info easier to read.
        """
        logging.debug('brick: %s', brick)
        ns.Touch.__init__(self, brick, port)
        ExternalInterface.__init__(self, None, **kwds)
        self.stopQueue = stopQueue
        self.resultQueue = resultQueue

    def run(self):
        button_down = self.is_pressed()
        while 1:
            time.sleep(random.random() * _usb_sleep_multiplier)
            # check for stop token:
            try:
                if self.stopQueue.get_nowait() is _stop_token:
                    logging.debug('received stop token')
                    self.stopQueue.put(_stop_token)
                    break
            except Queue.Empty:
                pass
            # get and push sensor state
            if not button_down and self.is_pressed():
                logging.debug('switched on')
                button_down = True
                self.resultQueue.put('start')
            elif button_down and not self.is_pressed():
                logging.debug('switched off')
                button_down = False
                self.resultQueue.put('stop')


class MotorRunThread(ExternalInterface, nm.Motor):
    """
    Motor running in its own thread. Takes work requests from
    self.workRequestQueue. Valid values are 'start' and 'stop'. Tacho
    readings pre start and post stop will be put into
    self.resultQueue.  """
    # assume all motors run in same direction with same power
    # NOTE: wrapping in list so it can be changed directly by other (sensor)
    # threads (a bit dirty but does the trick)
    power = [100]
    # time (in sec) to wait for request before continuing while loop
    reqWait = 0.2

    def __init__(self, brick, port, stopQueue, brake=False, **kwds):
        logging.debug('brick: %s', brick)
        nm.Motor.__init__(self, brick, port)
        self.stopQueue = stopQueue
        self.brake = brake
        ExternalInterface.__init__(self, None, **kwds)

    def run(self):
        while 1:
            # check for stop token
            try:
                if self.stopQueue.get_nowait() is _stop_token:
                    logging.debug('received stop token')
                    self.stopQueue.put(_stop_token)
                    break
            except Queue.Empty:
                pass
            # deal with action requests
            # time.sleep(random.random() * _usb_sleep_multiplier)
            try:
                action = self.workRequestQueue.get(
                        timeout=MotorRunThread.reqWait)
            except Queue.Empty:
                continue
            logging.debug("fetched action '%s'", action)
            if action == 'stop':
                if brake:
                    self.brake()
                else:
                    self.idle()
            try:
                t = self.get_tacho()
                self.resultQueue.put_nowait(t.tacho_count,
                        t.block_tacho_count, t.rotation_count))
            except Queue.Full:
                logging.debug('resultQueue full, could not push tacho'
                              ' reading with action: %s', action)
            if action == 'start':
                # 'run()'  conflicts with Thread.run(), resolve explicitly:
                nm.Motor.run(self, power=MotorRunThread.power[0])

class UltrasonicThread(ExternalInterface, ns.Ultrasonic):
    """
    Used to detect distance from wall. Will reverse direction by changing sign
    of motor's power parameter when distance falls below min_dist.
    """
    # distance at which to change direction
    min_dist = 10
    # grace period (in sec) after direction change:
    reverse_timout = 2

    def __init__(self, brick, port, powerList, resultQueue, stopQueue, **kwds):
        """
        Arguments:
        ----------
        brick : nxt.brick.Brick object
        port : int
            Port number to which sensor is connected
        resultQueue : Queue object
            Request queue of associated motor; will put 'action' keyword with
            'start' or 'stop' value on queue if button achtion is detected.
        stopQueue : Queue object
            run methos will poll this queue for _stop_token, ignoring
            everyting else. _stop_token will be put back onto queue before
            breaking.
        powerList : sequence
            Sign of powerList[0] will be changed if distance falls below
            min_dist.

        **kwds will be passed to Thread constructor and should include *name*
        keyword to make debugging info easier to read.
        """
        logging.debug('brick: %s', brick)
        ns.Ultrasonic.__init__(self, brick, port)
        self.stopQueue = stopQueue
        self.powerList = powerList
        ExternalInterface.__init__(self, None, **kwds)
        self.resultQueue = resultQueue

    def run(self):
        while 1:
            time.sleep(random.random() * _usb_sleep_multiplier)
            # check for stop token:
            try:
                if self.stopQueue.get_nowait() is _stop_token:
                    logging.debug('received stop token')
                    self.stopQueue.put(_stop_token)
                    break
            except Queue.Empty:
                pass
            # check and process distance:
            if self.get_distance() < UltrasonicThread.min_distance:
                logging.debug("direction reversal triggered")
                self.powerList[0] *= -1
                self.resultQueue.put('reverse')




class ResultQueueChecker(threading.Thread):
    """
    Can be run to keep a result queue tidy and log results.
    """
    # time (in sec) to wait for request before continuing while loop
    reqWait = 0.2

    def __init__(self, resQueue, stopQueue, **kwds):
        threading.Thread.__init__(self, **kwds)
        self.stopQueue = stopQueue
        self.setDaemon(1)
        self.resQueue = resQueue
        self.start()

    def run(self):
        while 1:
            # check for stop token
            try:
                if self.stopQueue.get_nowait() is _stop_token:
                    logging.debug('received stop token')
                    self.stopQueue.put(_stop_token)
                    break
            except Queue.Empty:
                pass
            # deal with results:
            try:
                result = self.resQueue.get(
                        timeout=ResultQueueChecker.reqWait)
            except Queue.Empty:
                continue
            logging.debug('fetched %s', result)


if __name__ == '__main__':

    try:
        b = find_one_brick(name='NAMANI')
    except BrickNotFoundError:
        if not b or type(b) != Brick:
            logging.warning("Couldn't find brick, exiting...")
            raise SystemExit
        else:
            pass

    logging.debug("Found brick: %s", b)

    t1 = ns.Touch(b, ns.PORT_1)
    t2 = ns.Touch(b, ns.PORT_2)

    ma = nm.Motor(b, nm.PORT_A)
    mb = nm.Motor(b, nm.PORT_B)

    pressed_1 = False
    pressed_2 = False

    try:
        while True:
            time.sleep(0.1)
            if t1.is_pressed() and not pressed_1:
                print "#1 pressed"
                pressed_1 = True
                ma.run(power=100)
            elif not t1.is_pressed() and pressed_1:
                print "#1 released"
                pressed_1 = False
                ma.idle()
            if t2.is_pressed() and not pressed_2:
                print "#2 pressed"
                pressed_2 = True
                mb.run(power=100)
            elif not t2.is_pressed() and pressed_2:
                print "#2 released"
                pressed_2 = False
                mb.idle()
    except KeyboardInterrupt:
        print "caught keyboard interrupt"
