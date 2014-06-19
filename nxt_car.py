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


def check_stop(stopQueue):
    try:
        if stopQueue.get_nowait() is _stop_token:
            stopQueue.put(_stop_token)
            return True
    except Queue.Empty:
        pass
    return False

# Callables for motor actions:
def motor_start(motor, power):
    t = motor.get_tacho()
    nm.Motor.run(motor, power=MotorRunThread.power[0])
    return t

def motor_stop(motor):
    if motor.brake_flag:
        motor.brake()
    else:
        motor.idle()
    return motor.get_tacho()

def motor_turn(motor, power, degrees):
    motor.turn(power, degrees)
    return motor.get_tacho()


class Serializer(threading.Thread):
    """
    Base class for actuator threads, taken from "Python in a Nutshell"
    """
    def __init__(self, **kwds):
        threading.Thread.__init__(self, **kwds)
        self.setDaemon(1)
        self.workRequestQueue = Queue.Queue( )
        self.resultQueue = Queue.Queue( )
        self.start( )

    def apply(self, callable, *args, **kwds):
        "called by other threads as callable would be"
        self.workRequestQueue.put((callable, args,kwds))
        return self.resultQueue.get( )

    def run(self):
        while 1:
            callable, args, kwds = self.workRequestQueue.get( )
            self.resultQueue.put(callable(*args, **kwds))


class SensorThreadBase(threading.Thread):
    """
    Minimalistic base class without queues. Needs to subclassed to do
    something useful.
    """
    def __init__(self, **kwds):
        threading.Thread.__init__(self, **kwds)
        self.setDaemon(1)
        self.start( )
    def run(self):
        while 1:
            time.sleep(random.random() * _usb_sleep_multiplier)
            if check_stop(self.stopQueue):
                logging.debug('received stop token')
                break


class MotorTouchThread(SensorThreadBase, ns.Touch):
    def __init__(self, brick, port, motor, stopQueue, **kwds):
        """
        Implememts start/stop touch sensor associated with motor.

        Arguments:
        ----------
        brick : nxt.brick.Brick object
        port : int
            Port number to which sensor is connected
        stopQueue : Queue object
            run methos will poll this queue for _stop_token, ignoring
            everyting else. _stop_token will be put back onto queue before
            breaking.
        motor : nxt motor object
            motor's `apply` method will be called to pass on work requests.

        **kwds will be passed to Thread constructor and should include *name*
        keyword to make debugging info easier to read.
        """
        logging.debug('brick: %s', brick)
        ns.Touch.__init__(self, brick, port)
        self.stopQueue = stopQueue
        self.motor = motor
        SensorThreadBase.__init__(self, **kwds)

    def run(self):
        button_down = self.is_pressed()
        while 1:
            time.sleep(random.random() * _usb_sleep_multiplier)
            if check_stop(self.stopQueue):
                logging.debug('received stop token')
                break
            # get and push sensor state
            if not button_down and self.is_pressed():
                logging.debug('switched on')
                button_down = True
                result = self.motor.apply(motor_start, self.motor,
                        self.motor.power[0])
                logging.debug('got motor result %s', result)
            elif button_down and not self.is_pressed():
                logging.debug('switched off')
                button_down = False
                result = self.motor.apply(motor_stop, self.motor)
                logging.debug('got motor result %s', result)


class MotorRunThread(Serializer, nm.Motor):
    """
    Motor running in its own thread. Takes work requests from
    self.workRequestQueue. Valid values are 'start' and 'stop'. Tacho
    readings pre start and post stop will be put into
    self.resultQueue.
    """
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
        self.brake_flag = brake
        Serializer.__init__(self, **kwds)

    def run(self):
        while 1:
            if check_stop(self.stopQueue):
                logging.debug('received stop token')
                break
            # deal with action requests
            # time.sleep(random.random() * _usb_sleep_multiplier)
            try:
                callable, args, kwds = self.workRequestQueue.get(
                        timeout=MotorRunThread.reqWait)
            except Queue.Empty:
                continue
            logging.debug("fetched %s %s %s", callable.__name__, args, kwds)
            self.resultQueue.put(callable(*args, **kwds))


class UltrasonicThread(SensorThreadBase, ns.Ultrasonic):
    """
    Used to detect distance from wall. Will reverse direction by changing sign
    of motor's power parameter when distance falls below min_distance.
    """
    # distance at which to change direction
    min_distance = 20
    # grace period (in sec) after direction change:
    reverse_timout = 2

    def __init__(self, brick, port, motor, powerList, stopQueue, **kwds):
        """
        Arguments:
        ----------
        brick : nxt.brick.Brick object
        port : int
            Port number to which sensor is connected
        motor : nxt motor object
            motor's `apply` method will be called to pass on work requests.
        stopQueue : Queue object
            run methos will poll this queue for _stop_token, ignoring
            everyting else. _stop_token will be put back onto queue before
            breaking.
        powerList : sequence
            Sign of powerList[0] will be changed if distance falls below
            min_distance.

        **kwds will be passed to Thread constructor and should include *name*
        keyword to make debugging info easier to read.
        """
        logging.debug('brick: %s', brick)
        ns.Ultrasonic.__init__(self, brick, port)
        self.motor = motor
        self.powerList = powerList
        self.stopQueue = stopQueue
        SensorThreadBase.__init__(self, **kwds)

    def run(self):
        while 1:
            time.sleep(random.random() * _usb_sleep_multiplier)
            if check_stop(self.stopQueue):
                logging.debug('received stop token')
                break
            # check and process distance:
            if self.get_distance() < UltrasonicThread.min_distance:
                logging.debug("direction reversal triggered")
                self.powerList[0] *= -1
                result = self.motor.apply(motor_turn, self.motor,
                        self.powerList[0], 180)
                logging.debug('got motor result %s', result)
                time.sleep(UltrasonicThread.reverse_timout)


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

    b = find_one_brick(name='NAMANI')

    logging.debug("Found brick: %s", b)

    # stopQueue:
    sq = Queue.Queue()


