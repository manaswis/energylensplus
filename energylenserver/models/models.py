from django.db import models

# Enable Logging
import logging
logger = logging.getLogger('energylensplus_django')

"""
Models for the rest of the application
"""

app_label_str = 'energylenserver'


class Devices(models.Model):

    dev_id = models.BigIntegerField(max_length=15,  primary_key=True)
    reg_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    creation_date = models.DateTimeField(auto_now_add=True)
    modified_date = models.DateTimeField(auto_now=True)
    phone_model = models.CharField(max_length=50)

    def __unicode__(self):
        return self.name

    class Meta:
        abstract = True
        ordering = ['-modified_date']


class RegisteredUsers(Devices):

    """
    Keeps track of all the registered users
    """
    apt_no = models.IntegerField()
    email_id = models.CharField(max_length=100, null=True)

    class Meta(Devices.Meta):
        db_table = 'registeredusers'
        app_label = app_label_str


class AccessPoints(models.Model):

    """
    Stores the access points for each apartment
    """
    apt_no = models.IntegerField()
    macid = models.CharField(max_length=200, )
    ssid = models.CharField(max_length=200)
    home_ap = models.BooleanField(default=False)

    def __unicode__(self):
        return self.ssid + "-" + self.macid

    class Meta:
        db_table = 'accesspoints'
        app_label = app_label_str


class Metadata(models.Model):

    """
    Stores the metadata for each apartment
    """
    apt_no = models.IntegerField()
    appliance = models.CharField(max_length=50)
    location = models.CharField(max_length=50)
    power = models.FloatField()
    presence_based = models.BooleanField(default=True)
    audio_based = models.BooleanField(default=True)
    how_many = models.IntegerField()

    def __unicode__(self):
        return self.dev_id.apt_no + "-" + self.appliance + "-" + self.location

    class Meta:
        db_table = 'metadata'
        app_label = app_label_str


class MeterInfo(models.Model):

    """
    Stores the meter details in each apartment
    """
    meter_uuid = models.CharField(max_length=255, primary_key=True)
    meter_type = models.CharField(max_length=20)
    apt_no = models.IntegerField()

    class Meta:
        db_table = 'meterinfo'
        app_label = app_label_str


class Edges(models.Model):

    """
    Stores the light and power edges from the smart meter data
    """
    timestamp = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    time = models.DateTimeField()
    magnitude = models.FloatField()
    type = models.CharField(max_length=10)
    curr_power = models.FloatField()
    meter = models.ForeignKey(MeterInfo)

    class Meta:
        db_table = 'edges'
        app_label = app_label_str
        get_latest_by = 'timestamp'


class EventLog(models.Model):

    """
    Stores all the detected events associated with the inferred "who", "what", "where" and "when"
    """
    edge = models.ForeignKey(Edges)
    event_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)  # when
    location = models.CharField(max_length=50)  # where
    appliance = models.CharField(max_length=50)  # what
    dev_id = models.ForeignKey(RegisteredUsers)  # who
    event_type = models.CharField(max_length=20)  # ON/OFF
    matched = models.BooleanField(default=False)  # Only for ON edges
    apt_no = models.IntegerField()

    class Meta:
        db_table = 'eventlog'
        app_label = app_label_str


class ActivityLog(models.Model):

    """
    Stores the inferred activities irrespective of the person responsible
    """
    start_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    end_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    appliance = models.CharField(max_length=50)
    location = models.CharField(max_length=50)
    power = models.FloatField()  # Average of magnitude of the matched edges
    usage = models.FloatField()  # Power * activity_duration (hours bw start and end time)
    meter = models.ForeignKey(MeterInfo)
    start_event = models.ForeignKey(EventLog, related_name=("ON event"))
    end_event = models.ForeignKey(EventLog, related_name=("OFF event"))
    report_sent = models.BooleanField(default=False)

    class Meta:
        db_table = 'activitylog'
        app_label = app_label_str


class EnergyUsageLog(models.Model):

    """
    Stores energy usage for each user for every activity
    """
    activity = models.ForeignKey(ActivityLog)
    start_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    end_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    stayed_for = models.DecimalField(unique=False, max_digits=10, decimal_places=3)
    usage = models.FloatField()
    dev_id = models.ForeignKey(RegisteredUsers)
    shared = models.BooleanField(default=False)

    class Meta:
        db_table = 'energyusagelog'
        app_label = app_label_str


class EnergyWastageLog(models.Model):

    """
    Stores energy wastage for each user for every activity
    """
    activity = models.ForeignKey(ActivityLog)
    start_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    end_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    left_for = models.DecimalField(unique=False, max_digits=10, decimal_places=3)
    wastage = models.FloatField()
    dev_id = models.ForeignKey(RegisteredUsers)

    class Meta:
        db_table = 'energywastagelog'
        app_label = app_label_str


class GroundTruthLog(models.Model):

    """
    Stores the submitted ground truth information for the inferred activities
    """
    by_dev_id = models.ForeignKey(RegisteredUsers, related_name=("Submitted by"))
    act_id = models.ForeignKey(ActivityLog)
    incorrect = models.BooleanField(default=False)  # if entry is incorrect
    start_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    end_time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    appliance = models.CharField(max_length=50)
    location = models.CharField(max_length=50)
    time_of_stay = models.DecimalField(unique=False, max_digits=10, decimal_places=3)
    occupant_dev_id = models.ForeignKey(RegisteredUsers, related_name=("Actual user"))

    class Meta:
        db_table = 'groundtruthlog'
        app_label = app_label_str


class EnergyWastageNotif(models.Model):

    """
    Stores the real-time wastage detected
    """
    dev_id = models.ForeignKey(RegisteredUsers)
    time = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    appliance = models.CharField(max_length=50)
    location = models.CharField(max_length=50)
    message = models.CharField(max_length=255)

    class Meta:
        db_table = 'energywastagenotif'
        app_label = app_label_str


"""
Models for maintaining usage stats for the mobile application
"""

import os
from django.db import connection


class UsageLogScreens(models.Model):

    """
    Stores the usage stats of each screen of the app
    """
    dev_id = models.ForeignKey(RegisteredUsers)
    time_of_day = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    screen_name = models.CharField(
        max_length=50)
    time_of_stay = models.DecimalField(unique=False, max_digits=10, decimal_places=3)

    def save_stats(self, user, filename):
        """
        Inserts csv into the database directly
        """
        try:
            cursor = connection.cursor()

            cursor.execute("LOAD DATA LOCAL INFILE %s INTO TABLE UsageLogScreens"
                           " FIELDS TERMINATED BY ',' IGNORE 1 LINES "
                           "(@timestamp, screen_name, @tos) "
                           "SET time_of_day = @timestamp/1000.0, "
                           "time_of_stay = @tos/1000, "
                           "dev_id_id = " + str(user.dev_id), [filename])
            os.remove(filename)
        except Exception, e:
            logger.error("[SaveStatsException] UsageLogScreens::%s", str(e))

    class Meta:
        db_table = 'usagelogscreens'
        app_label = app_label_str


class BatteryUsage(models.Model):

    """
    Stores the battery usage of the app
    """
    dev_id = models.ForeignKey(RegisteredUsers)
    timestamp = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    value = models.IntegerField()
    charging_state = models.BooleanField(default=False)
    scaled_usage = models.FloatField()

    def save_stats(self, user, filename):
        """
        Inserts csv into the database directly
        """
        try:
            cursor = connection.cursor()

            cursor.execute("LOAD data LOCAL INFILE %s INTO TABLE BatteryUsage"
                           " FIELDS TERMINATED BY ',' IGNORE 1 LINES "
                           "(@timestamp, value, @charging_state, scaled_usage) "
                           "SET timestamp = @timestamp/1000.0, "
                           "charging_state = @charging_state = 'true', "
                           "dev_id_id = " + str(user.dev_id), [filename])
            os.remove(filename)
        except Exception, e:
            logger.error("[SaveStatsException] BatteryUsage::%s", str(e))

    class Meta:
        db_table = 'batteryusage'
        app_label = app_label_str


class UsageLogNotifs(models.Model):

    """
    Stores the usage stats of each notification of the app
    """
    dev_id = models.ForeignKey(RegisteredUsers)
    received_at = models.DecimalField(unique=False, max_digits=14, decimal_places=3)
    notif_id = models.CharField(
        max_length=50)
    seen_at = models.DecimalField(unique=False, max_digits=10, decimal_places=3)

    def save_stats(self, user, filename):
        """
        Inserts csv into the database directly
        """
        try:
            cursor = connection.cursor()

            cursor.execute("LOAD DATA LOCAL INFILE %s INTO TABLE UsageLogNotifs"
                           " FIELDS TERMINATED BY ',' IGNORE 1 LINES "
                           "(@timestamp, notif_id, @seen_at) "
                           "SET received_at = @timestamp/1000.0, "
                           "seen_at = @seen_at/1000.0, "
                           "dev_id_id = " + str(user.dev_id), [filename])
            os.remove(filename)
        except Exception, e:
            logger.error("[SaveStatsException] UsageLogNotifs::%s", str(e))

    class Meta:
        db_table = 'usagelognotifs'
        app_label = app_label_str
