from django.core.management.base import NoArgsCommand
from ec2spotmanager.models import PoolConfiguration, InstancePool, Instance, INSTANCE_STATE
from django.conf import settings
from ec2spotmanager.management.common import mgmt_lock_required
import warnings
import time
import logging
import threading

from django.utils import timezone

from laniakea.laniakea import LaniakeaCommandLine
from laniakea.core.manager import Laniakea
import boto.ec2
import boto.exception

class Command(NoArgsCommand):
    help = "Check the status of all bugs we have"
    @mgmt_lock_required
    def handle_noargs(self, **options):
        while True:
            self.check_instance_pools()
            time.sleep(10)
    
    def check_instance_pools(self):
        instance_pools = InstancePool.objects.all()
        
        # Process all instance pools
        for instance_pool in instance_pools:
            config = instance_pool.config.flatten()
            instances = Instance.objects.filter(pool=instance_pool)
            
            instances_missing = config.size
            running_instances = []
            
            self.update_pool_instances(instance_pool, instances, config)
            for instance in instances:
                if instance.status_code == INSTANCE_STATE['running'] or instance.status_code == INSTANCE_STATE['pending']:
                    instances_missing -= 1
                    running_instances.append(instance)
                else:
                    # The instance is no longer running, delete it from our database
                    instance.delete()
            
            # Continue working with the instances we have running
            instances = running_instances
            
            if (not instance_pool.last_cycled) or instance_pool.last_cycled < timezone.now() - timezone.timedelta(seconds=config.cycle_interval):
                print("[Main] Pool needs to be cycled, terminating all instances...")
                instance_pool.last_cycled = timezone.now()
                self.terminate_pool_instances(instance_pool, instances, config, terminateByPool=True)
                instance_pool.save()
                self.update_pool_instances(instance_pool, instances, config)
                print("[Main] Pool termination complete.")
            
            # Determine which instances need to be cycled
            #outdated_instances = instances.filter(created__lt = timezone.now() - timezone.timedelta(seconds=config.cycle_interval))
            
            # Terminate all instances that need cycling
            #for instance in outdated_instances:
            #    self.terminate_instance(instance, config)
            #    instances_missing += 1
            
            if instances_missing > 0:
                print("[Main] Pool needs %s more instances, starting..." % instances_missing)
                self.start_pool_instances(instance_pool, config, count=instances_missing)
            elif instances_missing < 0:
                # Select the oldest instances we have running and terminate
                # them so we meet the size limitation again.
                print("[Main] Pool has %s instances over limit, terminating..." % -instances_missing)
                instances = Instance.objects.filter(pool=instance_pool).order_by('created')[:-instances_missing]
                self.terminate_pool_instances(instance_pool, instances, config)
            else:
                print("[Main] Pool size ok.")
                
    def get_best_region_zone(self, config):
        # TODO: Implement algorithm to determine best region/zone
        return ("us-east-1", "us-east-1d")
    
    def create_laniakea_images(self, config):
        images = { "default" : {} }
        
        # These are the configuration keys we want to put into the target configuration
        # without further preprocessing, except for the adjustment of the key name itself.
        keys = [
            'ec2_key_name',
            'ec2_image_name',
            'ec2_instance_type',
            'ec2_security_groups',
        ]
        
        for key in keys:
            lkey = key.replace("ec2_", "", 1)
            images["default"][lkey] = config[key]
        
        if config.ec2_raw_config:
            images["default"].update(config.ec2_raw_config)
        
        return images
    
    def start_pool_instances(self, pool, config, count=1):
        """ Start an instance with the given configuration """
        
        images = self.create_laniakea_images(config)
        (region, zone) = self.get_best_region_zone(config)
        instances = []
        
        # Create all our instances as pending, the async thread will update them once
        # they have been spawned.
        for i in range(0,count):
            instance = Instance()
            instance.ec2_region = region
            instance.status_code = INSTANCE_STATE["pending"]
            instance.pool = pool
            instance.save()
            instances.append(instance)
        
        # This method will run async to spawn our machines
        def start_instances_async(pool, config, count, images, region, zone, instances):
            userdata = LaniakeaCommandLine.handle_import_tags(config.ec2_userdata)
            userdata = LaniakeaCommandLine.handle_tags(userdata, config.ec2_userdata_macros)
            if not userdata:
                raise RuntimeError("start_instance: Failed to compile userdata")
            
            images["default"]['user_data'] = userdata
            images["default"]['placement'] = zone
            images["default"]['count'] = count
    
            cluster = Laniakea(images)
            try:
                cluster.connect(region=region, aws_access_key_id=config.aws_access_key_id, aws_secret_access_key=config.aws_secret_access_key)
            except Exception as msg:
                logging.error("%s: laniakea failure: %s" % ("start_instances_async", msg))
                return
            
            config.ec2_tags['SpotManager-PoolId'] = str(pool.pk)
    
            try:
                print("Creating %s instances" % count)
                boto_instances = cluster.create_spot(config.ec2_max_price, tags=config.ec2_tags)
                
                assert len(boto_instances) == len(instances) == count
                
                for i in range(0,count):
                    instances[i].hostname = boto_instances[i].public_dns_name
                    instances[i].ec2_instance_id = boto_instances[i].id
                    instances[i].status_code = boto_instances[i].state_code
                    instances[i].save()
                
            except boto.exception.EC2ResponseError as msg:
                logging.error("%s: boto failure: %s" % ("start_instances_async", msg))
                return
        
        # TODO: We don't get any information back from the async method call here, but should handle failures!
        t = threading.Thread(target=start_instances_async, args = (pool, config, count, images, region, zone, instances))
        t.start()
        
    def terminate_pool_instances(self, pool, instances, config, terminateByPool=False):
        """ Terminate an instance with the given configuration """        
        instance_ids_by_region = self.get_instance_ids_by_region(instances)
        
        for region in instance_ids_by_region:
            cluster = Laniakea(None)
            try:
                cluster.connect(region=region, aws_access_key_id=config.aws_access_key_id, aws_secret_access_key=config.aws_secret_access_key)
            except Exception as msg:
                logging.error("%s: laniakea failure: %s" % ("terminate_pool_instances", msg))
                return None
        
            try:
                if terminateByPool:
                    boto_instances = cluster.find(filters={"tag:SpotManager-PoolId" : str(pool.pk)})
                    
                    # Data consistency checks
                    for boto_instance in boto_instances:
                        assert ((boto_instance.id in instance_ids_by_region[region])
                                or (boto_instance.state_code == INSTANCE_STATE['shutting-down'] 
                                or boto_instance.state_code == INSTANCE_STATE['terminated']))
                        
                    cluster.terminate(boto_instances)
                else:
                    print("Terminating %s instances in region %s" % (len(instance_ids_by_region[region]),region))
                    cluster.terminate(cluster.find(instance_ids=instance_ids_by_region[region]))
            except boto.exception.EC2ResponseError as msg:
                logging.error("%s: boto failure: %s" % ("terminate_pool_instances", msg))
                return 1
    
    def get_instance_ids_by_region(self, instances):
        instance_ids_by_region = {}
        
        for instance in instances:
            if not instance.ec2_region in instance_ids_by_region:
                instance_ids_by_region[instance.ec2_region] = []
            instance_ids_by_region[instance.ec2_region].append(instance.ec2_instance_id)
            
        return instance_ids_by_region
    
    def get_instances_by_ids(self, instances):
        instances_by_ids = {}
        for instance in instances:
            instances_by_ids[instance.ec2_instance_id] = instance
        return instances_by_ids
    
    def update_pool_instances(self, pool, instances, config):
        """ Check the state of the instances in a pool and update it in the database """
        instance_ids_by_region = self.get_instance_ids_by_region(instances)
        instances_by_ids = self.get_instances_by_ids(instances)
        
        for region in instance_ids_by_region:
            cluster = Laniakea(None)
            try:
                cluster.connect(region=region, aws_access_key_id=config.aws_access_key_id, aws_secret_access_key=config.aws_secret_access_key)
            except Exception as msg:
                logging.error("%s: laniakea failure: %s" % ("update_pool_instances", msg))
                return None
        
            try:
                #cluster.find(instance_ids=instance_ids_by_region[region])
                boto_instances = cluster.find(filters={"tag:SpotManager-PoolId" : str(pool.pk)})
                
                for boto_instance in boto_instances:
                    # Whenever we see an instance that is not in our instance list for that region,
                    # make sure it's a terminated instance because we should never have running instance
                    if not boto_instance.id in instance_ids_by_region[region]:
                        assert (boto_instance.state_code == INSTANCE_STATE['shutting-down'] 
                            or boto_instance.state_code == INSTANCE_STATE['terminated'])
                        
                        continue
                    
                    instance = instances_by_ids[boto_instance.id]
                    
                    # Check the status code and update if necessary
                    if instance.status_code != boto_instance.state_code:
                        instance.status_code = boto_instance.state_code
                        instance.save()
                        
                    # If for some reason we don't have a hostname yet,
                    # update it accordingly.
                    if not instance.hostname:
                        instance.hostname = boto_instance.public_dns_name
                        instance.save()
                    
            except boto.exception.EC2ResponseError as msg:
                logging.error("%s: boto failure: %s" % ("update_pool_instances", msg))
                return 1
            
                    
