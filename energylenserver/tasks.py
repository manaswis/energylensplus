"""
[CONSUMER] Processes the data generated by the producer

1. Detect edges in real-time over streaming power data
2. Initiates the classification pipeline
3. Detects wastage/usage
4. Sends real-time feedback to user
"""

from __future__ import absolute_import

from energylenserver.setup_django_envt import *

import os
import time
import pandas as pd
import datetime as dt
from multiprocessing.managers import BaseManager

from celery import shared_task

"""
Imports from EnergyLens+
"""
# DB Imports
from energylenserver.models.models import *
from energylenserver.models.DataModels import *
from energylenserver.models import functions as mod_func

# Core Algo imports
from energylenserver.core import classifier
from energylenserver.core.constants import wastage_threshold
from energylenserver.core import functions as core_f
from energylenserver.meter import edge_detection
from energylenserver.core import user_attribution as attrib
from energylenserver.core import apportionment as apprt
from energylenserver.meter import edge_matching as e_match

# GCM Client imports
from energylenserver.gcmxmppclient.messages import create_message

# Reporting API imports
from energylenserver.api import reporting as rpt

# Common imports
from energylenserver.common_imports import *
from energylenserver.constants import apt_no_list

# Enable Logging
logger = logging.getLogger('energylensplus_django')
elogger = logging.getLogger('energylensplus_error')
meter_logger = logging.getLogger('energylensplus_meterdata')


# Global variables
# Model mapping with filenames

FILE_MODEL_MAP = {
    'wifi': (WiFiTestData, "WiFiTestData"),
    'rawaudio': (RawAudioTestData, "RawAudioTestData"),
    'audio': (MFCCFeatureTestSet, "MFCCFeatureTestSet"),
    'accelerometer': (AcclTestData, "AcclTestData"),
    'light': (LightTestData, "LightTestData"),
    'mag': (MagTestData, "MagTestData"),
    'Trainingwifi': (WiFiTrainData, "WiFiTrainData"),
    'Trainingrawaudio': (RawAudioTrainData, "RawAudioTrainData"),
    'Trainingaudio': (MFCCFeatureTrainSet, "MFCCFeatureTrainSet"),
    'Trainingaccelerometer': (AcclTrainData, "AcclTrainData"),
    'Traininglight': (LightTrainData, "LightTrainData"),
    'Trainingmag': (MagTrainData, "MagTrainData")
}


class ClientManager(BaseManager):
    pass

# '''
# Establishing connection with the running GCM Server
try:
    ClientManager.register('get_client_obj')
    manager = ClientManager(address=('localhost', 50000), authkey='abracadabra')
    manager.connect()
    client = manager.get_client_obj()
    if client is None or client == "":
        logger.debug("GCM Client not connected")
    else:
        logger.debug("Got the GCM Client: client obj type:: %s", type(client))
except Exception, e:
    elogger.error("[InternalGCMClientConnectionException] %s", e)
# '''

"""
Data Handlers
"""


@shared_task
def phoneDataHandler(filename, sensor_name, filepath, training_status, user):
    """
    Consumes sensor data streams from the phone
    Performs some preprocessing and inserts records
    into the database

    :param filename:
    :param sensor_name:
    :param df_csv:
    :param training_status:
    :param user:
    :return upload status:
    """

    logger.debug("FILE:: %s", filename)

    try:
        # Create a dataframe for preprocessing
        if sensor_name != 'rawaudio':
            try:
                df_csv = pd.read_csv(filepath)
            except Exception, e:
                logger.error("[InsertDataException]::%s", str(e))
                os.remove(filepath)
                return

        # Remove rows with 'Infinity' in MFCCs created
        if sensor_name == 'audio':
            if str(df_csv.mfcc1.dtype) != 'float64':
                df_csv = df_csv[df_csv.mfcc1 != '-Infinity']

        if sensor_name != 'rawaudio':
            # logger.debug("Total number of records to insert: %d", len(df_csv))

            # Remove NAN timestamps
            df_csv.dropna(subset=[0], inplace=True)

            # Create temp csv file
            os.remove(filepath)
            df_csv.to_csv(filepath, index=False)

        # --Initialize Model--
        if training_status is True:
            model = FILE_MODEL_MAP['Training' + sensor_name]
        else:
            model = FILE_MODEL_MAP[sensor_name]

        # --Store data in the model--
        model[0]().insert_records(user, filepath, model[1])

        logger.debug("Successful Upload! File: %s", filename)
    except Exception, e:
        logger.debug("[PhoneDataHandlerException]:: %s", e)


@shared_task
def meterDataHandler(df, file_path):
    """
    Consumes sensor data streams from the meter
    """

    meter_uuid_folder = os.path.dirname(file_path)
    uuid = meter_uuid_folder.split('/')[-1]
    meter_logger.debug("Detecting Edges for UUID:: %s", uuid)

    # -- Detect Edge --
    edges_df = edge_detection.detect_and_filter_edges(df)
    # Store edges into db

    if len(edges_df) == 0:
        meter_logger.debug("No edges detected")
        return

    # For the detected edge, store edge and call classification pipeline task
    for idx in edges_df.index:
        edge = edges_df.ix[idx]
        edge_time = edge.time

        try:

            meter = MeterInfo.objects.get(meter_uuid=uuid)
            # Check if the edge exists
            try:
                record = Edges.objects.get(meter=meter, timestamp=edge_time)
            except Edges.DoesNotExist, e:

                # --Store edge--
                edge_r = Edges(timestamp=int(edge_time), time=dt.datetime.fromtimestamp(edge_time),
                               magnitude=edge.magnitude, type=edge.type,
                               curr_power=edge.curr_power, meter=meter)
                edge_r.save()

                meter_logger.debug("Edge for UUID: %s at [%s] of mag %d", uuid, time.ctime(
                    edge['time']), edge['magnitude'])

                # Initiate classification pipeline
                edgeHandler(edge_r)
        except Exception, e:
            meter_logger.error("[EdgeSaveException]:: %s", str(e))


def edgeHandler(edge):
    """
    Starts the classification pipeline and relays edges based on edge type
    """
    logger.debug("Starting the Classification pipeline..")
    if edge.type == "falling":
        chain = (classify_edge.s(edge) |
                 find_time_slice.s() | apportion_energy.s())
    else:
        chain = classify_edge.s(edge)
    chain()
    logger.debug("Classification Pipeline ended for edge: [%s] :: %d",
                 time.ctime(edge.timestamp), edge.magnitude)

"""
Invokes the EnergyLens+ core algorithm
"""


@shared_task
def classify_edge(edge):
    """
    Consumes smart meter edges and phone data to give out 'who',
    'what', 'where' and 'when'
    :param edge:
    :return "where", what" and "who" labels:
    """

    try:

        apt_no = edge.meter.apt_no
        logger.debug("Apt.No.:: %d Classify edge type '%s' [%s] %d",
                     apt_no, edge.type, time.ctime(edge.timestamp), edge.magnitude)

        # Defining event window
        p_window = 60  # window for each side of the event time (in seconds)

        event_time = edge.timestamp
        magnitude = edge.magnitude

        start_time = event_time - p_window
        end_time = event_time + p_window

        # --- Preprocessing ---
        # Step 2: Determine user at home
        user_list = core_f.determine_user_home_status(start_time, end_time, apt_no)
        n_users_at_home = len(user_list)

        if n_users_at_home == 0:
            logger.debug("No user at home. Ignoring edge activity.")
            return 'ignore', 'ignore', 'ignore', 'ignore'

        # --- Classification --
        location_dict = {}
        appliance_dict = {}

        # Get user details
        users = mod_func.get_users(user_list)
        for user in users:
            dev_id = user.dev_id

            # Step 1: Determine location for every user
            location = classifier.classify_location(apt_no, start_time, end_time, user)
            if location:
                location_dict[dev_id] = location

            # Step 2: Determine appliance for every user
            appliance = classifier.classify_appliance(apt_no, start_time, end_time, user)
            if appliance:
                appliance_dict[dev_id] = appliance

        logger.debug("Determined Locations: %s", location_dict)
        logger.debug("Determined Appliances: %s", appliance_dict)

        # Step 3: Determine user based on location, appliance and metadata
        if n_users_at_home > 1:
            user = attrib.identify_user(apt_no, magnitude, location_dict, appliance_dict, user_list)
            who = user['dev_id']
            where = user['location']
            what = user['appliance']

        elif n_users_at_home == 1:
            user = user_list[0]
            who = user_list
            where = location_dict[user]
            what = appliance_dict[user]

        logger.debug("[%s] :: Determined labels: %s %s %s" %
                     (time.ctime(event_time), who, where, what))

        if edge.type == "rising":
            event_type = "ON"
        else:
            event_type = "OFF"

        if isinstance(who, list):

            # New record for each user for an edge - indicates multiple occupants
            # were present in the room during the event
            for user in who:
                # Create a record in the Event Log with edge id
                # and store 'who', 'what', 'where' labels
                event = EventLog(edge=edge, event_time=event_time,
                                 location=where, appliance=what, dev_id=user,
                                 event_type=event_type)
                event.save()
                # ONLY FOR TESTING
                message = "%s uses %s in %s" % (who, what, where)
                send_notification(user, message)
        elif isinstance(who, str):
            # Create a record in the Event Log with edge id
            # and store 'who', 'what', 'where' labels
            event = EventLog(edge=edge, event_time=event_time,
                             location=where, appliance=what, dev_id=user,
                             event_type=event_type)
            event.save()
    except Exception, e:
        logger.error("[ClassifyEdgeException]:: %s", e)
        return 'ignore', 'ignore', 'ignore', 'ignore'

    return who, what, where, event


@shared_task
def find_time_slice(result_labels):
    """
    Consumes "where", what" and "who" labels and gives out "when"
    and stores an "activity"
    Runs when an OFF event is detected
    :param edge:
    :param who:
    :param what:
    :param where:
    :return when:
    """
    return_error = 'ignore', 'ignore', 'ignore', 'ignore'
    try:
        who, what, where, off_event = result_labels

        # If no user at home or no identified users, skip off_event
        if (who == 'ignore' and what == 'ignore' and
            where == 'ignore') or (who == 'not_found' and
                                   what == 'not_found' and
                                   where == 'not_found'):
            return return_error

        apt_no = off_event.edge.meter.apt_no
        off_time = off_event.event_time
        magnitude = off_event.edge.magnitude

        logger.debug("Determining activity duration: [%s] :: %s" % (
            time.ctime(off_time), str(magnitude)))

        # Match ON/OFF events
        matched_on_event = e_match.match_events(apt_no, off_event)

        if isinstance(matched_on_event, bool):
            logger.debug("No ON event found")
            return return_error

        # Mark ON event as matched
        matched_on_event.matched = True
        matched_on_event.save()

        off_event.matched = True
        off_event.save()

        # Inferred activity time slice
        start_time = matched_on_event.event_time
        end_time = off_time

        power = round((magnitude + matched_on_event.edge.magnitude) / 2)
        usage = apprt.get_energy_consumption(start_time, end_time, power)

        # Save Activity
        activity = ActivityLog(start_time=start_time, end_time=end_time,
                               location=where, appliance=what,
                               power=power, usage=usage,
                               start_event=matched_on_event.edge,
                               end_event=off_event.edge)
        activity.save()

        logger.debug("Time slice for activity: %s uses %s in %s between %s and %s",
                     who, what, where, time.ctime(start_time), time.ctime(end_time))
    except Exception, e:
        logger.error("[FindTimeSliceException]:: %s", str(e))
        return return_error

    return apt_no, start_time, end_time, activity

"""
Invokes the components that use EnergyLens+ outputs - who, what, where and when:
1. Wastage Detection
2. Energy Apportionment
"""


@shared_task
def apportion_energy(result_labels):
    """
    Determines the energy usage/wastage of an individual based on
    activity parameters and length of stay for each activity

    :return: energy usage
    """
    try:
        logger.debug("Apportioning energy..")

        apt_no, start_time, end_time, activity = result_labels

        if (start_time == 'ignore' and end_time == 'ignore' and
                activity == 'ignore'):
            logger.debug("Ignoring request")
            return

        logger.debug("Apportioned energy:: [%s] :: %s" % (
            time.ctime(event_time), str(magnitude)))

        user_list = core_f.determine_user_home_status(start_time, end_time, apt_no)
        n_users_at_home = len(user_list)

        if n_users_at_home == 0:
            logger.error("No user at home. Something went wrong!")
            return

        act_loc = activity.location

        presence_df = pd.DataFrame(columns=['start_time', 'end_time'])
        for user in user_list:

            user_id = user.dev_id

            # Determine duration of stay in the room for a user
            df = core_f.get_presence_matrix(
                apt_no, user, start_time, end_time, act_loc)
            presence_df[str(user_id)] = df['label']

        # Merge slices where the user columns have the same values
        presence_df = core_f.merge_presence_matrix(presence_df)

        # Determine actual usage/wastage of a user based on
        # time of stay in the room of activity handling all complex cases
        apprt.calculate_consumption(user_list, presence_df, activity)

    except Exception, e:
        logger.error("[ApportionEnergyException]:: %s", str(e))


@shared_task
def determine_wastage(apt_no):
    """
    Determines energy wastage in real-time
    """
    try:
        end_time = int(time.time())
        start_time = end_time - wastage_threshold

        # Determine users at home
        user_list = core_f.determine_user_home_status(start_time, end_time, apt_no)
        n_users_at_home = len(user_list)

        if n_users_at_home == 0:
            logger.debug("No users at home")
            return

        # Build presence matrix for the ongoing activities
        on_event_records = mod_func.get_on_events(apt_no, end_time)
        users = mod_func.get_users(user_list)
        for event in on_event_records:

            what = event.appliance
            where = event.location

            presence_df = pd.DataFrame(columns=['start_time', 'end_time'])
            for user in users:
                user_id = user.dev_id

                # Build presence matrix
                df = core_f.get_presence_matrix(apt_no, user_id, start_time, end_time, where)
                presence_df[str(user_id)] = df['label']

            # Merge slices where the user columns have the same values
            presence_df = core_f.merge_presence_matrix(presence_df)

            # Determine wastage - find rooms of activity that are empty
            user_columns = presence_df.columns - ['start_time', 'end_time']
            col_sum = presence_df.ix[:, user_columns].sum(axis=1, numeric_only=True)
            w_slices_ix = presence_df.index[np.where(col_sum == 0)[0]]

            # Save and send notifications to all the users
            if len(w_slices_ix) > 0:
                message = "Energy wastage detected in %s! %s left ON." % (
                    where, what)
                # Save
                for user in users:
                    wastage = EnergyWastageNotif(dev_id=user, time=time.time(),
                                                 appliance=what,
                                                 location=where,
                                                 message=message)
                    wastage.save()

                # Inform
                inform_all_users(apt_no, message, users)

    except Exception, e:
        logger.debug("[DetermineWastageRTException]:: %s", e)


@shared_task(name='tasks.realtime_wastage_notif')
def realtime_wastage_notif():
    """
    Determines energy wastage in real-time and sends notification to all the
    users
    """
    try:
        # Determine wastage for each apt no independently
        for apt_no in apt_no_list:
            determine_wastage.delay(apt_no)

    except Exception, e:
        logger.error("DetermineWastageException]:: %s", str(e))


"""
Invokes the real-time feedback component
"""


@shared_task(name='tasks.send_validation_report')
def send_validation_report():
    """
    Sends a ground truth validation report to all the users
    """
    logger.debug("Sending periodic validation report..")
    try:
        # Get all the users
        users = mod_func.get_all_active_users()
        if users is False:
            logger.debug("No users are active")
            return
        for user in users:
            reg_id = user.reg_id
            apt_no = user.apt_no
            dev_id = user.dev_id

            # Construct the message
            data_to_send = {}
            data_to_send['msg_type'] = "response"
            data_to_send['api'] = GROUND_TRUTH_NOTIF_API
            data_to_send['options'] = {}
            activities = rpt.get_inferred_activities(dev_id)

            if isinstance(activities, bool):
                return

            if len(activities) == 0:
                return

            appliances = []
            records = mod_func.retrieve_metadata(apt_no)
            if records:
                for r in records:
                    appliances.append({'location': r.location, 'appliance': r.appliance})
            appliances.append({'location': "Unknown", 'appliance': "Unknown"})
            '''
            appliances.append({'location': "Bedroom", 'appliance': "TV"})
            appliances.append({'location': "Dining Room", 'appliance': "TV"})
            appliances.append({'location': "Study", 'appliance': "Computer"})
            '''

            users = mod_func.retrieve_users(apt_no)

            occupants = {}
            for user_i in users:
                occupants[user_i.dev_id] = user_i.name

            data_to_send['options']['activities'] = activities
            data_to_send['options']['appliances'] = appliances
            data_to_send['options']['occupants'] = occupants

            # Send the message
            send_notification(reg_id, data_to_send)
            logger.debug("Sent report to:: %s", user.name)
    except Exception, e:
        logger.debug("[SendingReportException]:: %s", e)


@shared_task
def inform_all_users(apt_no, notif_message, users):
    """
    Informs the user by sending a notification to all the users
    :return message:
    """

    import random
    import string

    try:
        # Create notification for active users
        for user in users:
            reg_id = user.reg_id
            notif_id = random.choice(string.digits)

            # notif_message = ('Please turn off the Light in the Bedroom' + str(notif_id)) * 3
            # Construct the message
            message_to_send = {}
            message_to_send['msg_type'] = 'response'
            message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
            message_to_send['options'] = {}
            message_to_send['options']['id'] = notif_id
            message_to_send['options']['message'] = notif_message

            # Send the message
            time_to_live = 30 * 60
            send_notification(reg_id, message_to_send, time_to_live)
            logger.debug("Notified user :: %s", user.name)
    except Exception, e:
        logger.debug("[InformAllUsersException]:: %s", e)


@shared_task
def inform_user(dev_id, notif_message):
    """
    Sends a notification to a specific user
    """
    try:

        # Get user
        user = mod_func.get_user(dev_id)
        if users is False:
            logger.error("Specified user does not exist")
            return

        reg_id = user.reg_id
        notif_id = random.choice(string.digits)

        # Construct the message
        message_to_send = {}
        message_to_send['msg_type'] = 'response'
        message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
        message_to_send['options'] = {}
        message_to_send['options']['id'] = notif_id
        message_to_send['options']['message'] = notif_message

        # Send the message
        send_notification(reg_id, message_to_send)
        logger.debug("Notified user :: %s", user.name)
    except Exception, e:
        logger.debug("[InformUserException]:: %s", e)


@shared_task
def send_notification(reg_id, message_to_send, time_to_live=3600):
    """
    Sends a message to a specific user
    """
    try:
        message = create_message(reg_id, message_to_send, time_to_live)
        client.send_message(message)
    except Exception, e:
        logger.debug("[SendingNotifException]:: %s", e)

"""
Test Task
"""


@shared_task
def send_msg(reg_id):
    logger.debug("Sending Message at [%s]", time.ctime(time.time()))
    message_to_send = {}
    message_to_send['msg_type'] = 'response'
    message_to_send['api'] = ENERGY_WASTAGE_NOTIF_API
    message_to_send['options'] = {}
    message_to_send['options']['message'] = 'Please turn off the Light in the Bedroom'
    message = create_message(reg_id, message_to_send)
    client.send_message(message)
    logger.debug("Message Sent at [%s]", time.ctime(time.time()))


'''
Test Tasks
@shared_task
def test_task(x):
    logger.debug("X=" + str(x)
    relay(x)


def relay(x):
    logger.debug("Called relay"
    chain = add.s(x, 10) | mul.s(1001)
    chain()

@shared_task
def add(x,y):
    logger.debug("Called Add"
    logger.debug("x="+ str(x) + " y=" + str(y)
    z = x+y
    return x,y,z

@shared_task
def mul(s, n):
    logger.debug("Called mul"
    print n
    print s
    (x,y,z) = s
    logger.debug("x="+ str(x) + " y=" + str(y) + " z=" + str(z)
    w = x * y * z
    logger.debug("W=" + w
'''
