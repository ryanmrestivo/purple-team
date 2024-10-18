from django.db import models
from picklefield.fields import PickledObjectField
from .base_model import BaseModel
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType

import pdb
from django.db.models.signals import pre_save, post_save
from django.db.models import Q
from django.dispatch import receiver
from armory2.armory_main.included.utilities.color_display import (
    display,
    display_warning,
    display_new,
    display_error,
)
from armory2.armory_main.included.utilities.network_tools import (
    validate_ip,
    get_ips,
    private_subnets,
)

from netaddr import IPNetwork, IPAddress as IPAddr
from ipwhois import IPWhois
import tld


class ToolRun(BaseModel):
    args = models.CharField(max_length=1024, default="")
    port = models.IntegerField(default=0)
    port_obj = models.ForeignKey(
        "Port", on_delete=models.CASCADE, blank=True, null=True
    )
    tool = models.CharField(max_length=128)
    virtualhost = models.ForeignKey(
        "VirtualHost", on_delete=models.CASCADE, blank=True, null=True
    )

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True)
    object_id = models.PositiveIntegerField(null=True)
    content_object = GenericForeignKey()


class BaseDomain(BaseModel):
    name = models.CharField(max_length=64)
    dns = PickledObjectField(default=dict)
    toolrun = GenericRelation(ToolRun, related_query_name="base_domains")

    def __str__(self):
        return self.name


class CIDR(BaseModel):
    name = models.CharField(max_length=44, unique=True)
    org_name = models.CharField(max_length=256, unique=False, null=True)
    toolrun = GenericRelation(ToolRun, related_query_name="cidrs")

    def __str__(self):
        return "{}: {}".format(self.name, self.org_name)


class Domain(BaseModel):
    name = models.CharField(max_length=128, unique=True)
    ip_addresses = models.ManyToManyField("IPAddress")
    basedomain = models.ForeignKey(BaseDomain, on_delete=models.CASCADE)
    whois = models.TextField()
    toolrun = GenericRelation(ToolRun, related_query_name="domains")

    def __str__(self):
        return self.name


class IPAddress(BaseModel):
    ip_address = models.CharField(max_length=39, unique=True)
    cidr = models.ForeignKey(CIDR, on_delete=models.CASCADE)
    os = models.CharField(max_length=512)
    whois = models.TextField()
    version = models.IntegerField()
    notes = models.TextField(default="")
    completed = models.BooleanField(default=False, null=True)
    toolrun = GenericRelation(ToolRun, related_query_name="ip_addresses")

    def __str__(self):
        return self.ip_address

    def add_tool_run(self, tool, args="", port=None, virtualhost=None):
        port_obj = None
        if port:
            port_objs = Port.objects.filter(
                ip_address=self, port_number=port, proto="tcp"
            )
            if port_objs.exists():
                port_obj = port_objs[0]
        if virtualhost:
            vhost, created = VirtualHost.objects.get_or_create(
                name=virtualhost, ip_address=self, port=port_obj
            )
        else:
            vhost = None
        self.toolrun.get_or_create(
            tool=tool, args=args, port_obj=port_obj, virtualhost=vhost
        )

    def get_virtualhosts(self):
        return sorted(
            list(
                set(
                    [
                        vh.name
                        for vh in VirtualHost.objects.filter(
                            ip_address=self, active=True
                        )
                    ]
                )
            )
        )

    @classmethod
    def get_sorted(
        cls, scope_type=None, search=None, display_zero=False, page_num=1, entries=50
    ):
        if scope_type == "active":
            qry = cls.objects.filter(active_scope=True)
        elif scope_type == "passive":
            qry = cls.objects.filter(passive_scope=True)
        else:
            qry = cls.objects.all()

        if not display_zero:
            qry = qry.filter(port__port_number__gt=0).distinct()

        if search:
            qry = qry.filter(
                Q(ip_address__icontains=search) | Q(domain__name__icontains=search)
            )

        res = []
        total = qry.count()

        # pdb.set_trace()
        return (
            qry.order_by("ip_address")[(page_num - 1) * entries : page_num * entries],
            total,
        )


class VirtualHost(BaseModel):
    ip_address = models.ForeignKey(IPAddress, on_delete=models.CASCADE)
    name = models.CharField(max_length=256)
    port = models.ForeignKey("Port", on_delete=models.CASCADE, blank=True, null=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.ip_address}[{self.name}]"


class Port(BaseModel):
    port_number = models.IntegerField(unique=False)
    proto = models.CharField(max_length=32)
    status = models.CharField(max_length=32, default="open")
    service_name = models.CharField(max_length=256)
    ip_address = models.ForeignKey(IPAddress, on_delete=models.CASCADE)
    cert = models.TextField(unique=False, null=True)
    certs = PickledObjectField(default=dict)
    info = PickledObjectField(default=dict)
    # toolrun = GenericRelation(ToolRun, related_query_name="ports")

    def __str__(self):
        return "{} / {} / {}".format(self.proto, self.port_number, self.service_name)

    def get_active_virtualhosts(self):
        return self.virtualhost_set.filter(active=True).order_by("name")

    class Meta:
        ordering = ["port_number"]


# pre_save.connect(Domain.pre_save, sender=Domain)


@receiver(pre_save, sender=BaseDomain)
def pre_save_basedomain(sender, instance, *args, **kwargs):
    if not instance.id:
        display_new(
            "New base domain added: {}  Active Scope: {}    Passive Scope: {}".format(
                instance.name, instance.active_scope, instance.passive_scope
            )
        )


@receiver(pre_save, sender=Domain)
def pre_save_domain(sender, instance, *args, **kwargs):
    if not instance.id:
        domain_name = "".join(
            [
                i
                for i in instance.name.lower()
                if i in "abcdefghijklmnopqrstuvwxyz.-0123456789"
            ]
        )
        if domain_name.count(".") < 1:
            domain_name = domain_name + ".badfqdn.local"

        try:
            base_domain = tld.get_fld(f"http://{domain_name}")
        except Exception as e:
            # if tld fails try to extract the basedomain out of the hostname
            if domain_name.count(".") == 1:
                base_domain = domain_name
            elif domain_name.count(".") == 2:
                base_domain = domain_name.partition(".")[2]
            elif domain_name.count(".") == 3:
                base_domain = domain_name.partition(".")[2].partition(".")[2]
            else:
                base_domain = "local"

        bd, created = BaseDomain.objects.get_or_create(
            name=base_domain,
            defaults={
                "active_scope": instance.active_scope,
                "passive_scope": instance.passive_scope,
            },
        )

        if not created:
            instance.passive_scope = bd.passive_scope
            instance.active_scope = bd.active_scope

        instance.basedomain = bd

        display_new(
            "New domain added: {}  Active Scope: {}    Passive Scope: {}".format(
                instance.name, instance.active_scope, instance.passive_scope
            )
        )


@receiver(post_save, sender=Domain)
def post_save_domain(sender, instance, created, *args, **kwargs):
    if "offlinedns" in instance.meta:
        return
    if created:
        domain_name = instance.name
        ips = get_ips(domain_name)

        for i in ips:
            ip, created = IPAddress.objects.get_or_create(ip_address=i)

            if ip.active_scope or instance.active_scope:
                instance.active_scope = True
                ip.active_scope = True

            if instance.passive_scope or ip.passive_scope:
                instance.passive_scope = True
                ip.passive_scope = True

            for p in ip.port_set.all():
                vh, created = VirtualHost.objects.get_or_create(
                    ip_address=ip, port=p, name=domain_name
                )
            display_new(
                "IP and Domain {}/{} scope updated to:  Active Scope: {}     Passive Scope: {}".format(
                    i, domain_name, ip.active_scope, ip.passive_scope
                )
            )

            ip.save()
            vh, created = VirtualHost.objects.get_or_create(
                ip_address=ip, port=None, name=instance.name
            )
            if created:
                display_new(
                    f"Added {instance.name} to virtualhosts for {ip.ip_address}"
                )
            instance.ip_addresses.add(ip)
            for p in ip.port_set.filter(service_name__icontains="http"):
                vh, created = VirtualHost.objects.get_or_create(
                    ip_address=ip, port=p, name=instance.name
                )
                if created:
                    display_new(
                        f"Added {instance.name} to virtualhosts for {ip.ip_address}:{p.port_number}"
                    )
            instance.save()


@receiver(pre_save, sender=IPAddress)
def pre_save_ip(sender, instance, *args, **kwargs):
    if not instance.id:
        res = validate_ip(instance.ip_address)
        if res == "ipv4":
            instance.version = 4
        elif res == "ipv6":
            instance.version = 6
        else:
            raise Exception("Not a valid IPv4 or IPv6 address.")

        # addr = IPAddress(instance.ip_address)

        cidrs = CIDR.objects.all()

        for c in cidrs:
            if instance.ip_address in IPNetwork(c.name):
                instance.active_scope = c.active_scope
                instance.passive_scope = c.passive_scope
                instance.cidr = c
                break

        try:
            cidr = instance.cidr
        except CIDR.DoesNotExist:
            cidr_data, org_name = get_cidr_info(instance.ip_address)
            cidr, created = CIDR.objects.get_or_create(
                name=cidr_data, defaults={"org_name": org_name}
            )
            instance.cidr = cidr
        display_new(
            "New IP added: {}  Active Scope: {}    Passive Scope: {}".format(
                instance.ip_address, instance.active_scope, instance.passive_scope
            )
        )


@receiver(post_save, sender=Port)
def post_save_port(sender, instance, created, *args, **kwargs):
    if created:
        for vhost in instance.ip_address.virtualhost_set.all():
            vh, created = VirtualHost.objects.get_or_create(
                ip_address=instance.ip_address, port=instance, name=vhost.name
            )


@receiver(pre_save, sender=CIDR)
def pre_save_cidr(sender, instance, *args, **kwargs):
    if not instance.id and not instance.org_name:
        cidr_data, org_name = get_cidr_info(instance.name.split("/")[0])

        instance.org_name = org_name

    if not instance.id:
        display_new(
            "New CIDR added: {} - {} Active Scope: {}    Passive Scope: {}".format(
                instance.name,
                instance.org_name,
                instance.active_scope,
                instance.passive_scope,
            )
        )


def get_cidr_info(ip_address):
    for p in private_subnets:
        if ip_address in p:
            return str(p), "Non-Public Subnet"

    try:
        res = IPWhois(ip_address).lookup_whois(get_referral=True)
    except Exception:
        try:
            res = IPWhois(ip_address).lookup_whois()
        except Exception as e:
            display_error("Error trying to resolve whois: {}".format(e))
            res = {}
    if not res.get("nets", []):
        display_warning("The networks didn't populate from whois. Defaulting to a /24.")
        # again = raw_input("Would you like to try again? [Y/n]").lower()
        # if again == 'y':
        #     time.sleep(5)
        # else:

        return (
            "{}.0/24".format(".".join(ip_address.split(".")[:3])),
            "Whois failed to resolve.",
        )

    cidr_data = []

    for net in res["nets"]:
        for cd in net["cidr"].split(", "):
            cidr_data.append(
                [
                    len(IPNetwork(cd)),
                    cd,
                    net["description"] if net["description"] else "",
                ]
            )
    try:
        cidr_data.sort()
    except Exception as e:
        display_error("Error occured: {}".format(e))
        pdb.set_trace()
    return cidr_data[0][1], cidr_data[0][2]
