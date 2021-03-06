from django.shortcuts import render, redirect, get_object_or_404
from ec2spotmanager.models import InstancePool, PoolConfiguration, Instance,\
    INSTANCE_STATE_CODE
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import logout
from django.db.models.aggregates import Count

def renderError(request, err):
    return render(request, 'error.html', { 'error_message' : err })

def logout_view(request):
    logout(request)
    return redirect('ec2spotmanager:index')

@login_required(login_url='/login/')
def index(request):
    return redirect('ec2spotmanager:pools')

@login_required(login_url='/login/')
def pools(request):
    filters = {}
    isSearch = True
    
    entries = InstancePool.objects.annotate(size=Count('instance')).order_by('-id')
    
    #(user, created) = User.objects.get_or_create(user = request.user)
    #defaultToolsFilter = user.defaultToolsFilter.all()
    #if defaultToolsFilter:
    #    entries = entries.filter(reduce(operator.or_, [Q(("tool",x)) for x in defaultToolsFilter]))
    
    # These are all keys that are allowed for exact filtering
    exactFilterKeys = [
                       "config__name",
                       ]
    
    for key in exactFilterKeys:
        if key in request.GET:
            filters[key] = request.GET[key]
    
    # If we don't have any filters up to this point, don't consider it a search
    if not filters:        
        isSearch = False
    
    entries = entries.filter(**filters)
    data = { 'isSearch' : isSearch, 'poollist' : entries }
    
    return render(request, 'pools/index.html', data)


@login_required(login_url='/login/')
def viewPool(request, poolid):
    pool = get_object_or_404(InstancePool, pk=poolid)
    instances = Instance.objects.filter(pool=poolid)
    
    for instance in instances:
        instance.status_code_text = INSTANCE_STATE_CODE[instance.status_code]
    
    last_config = pool.config
    last_config.child = None
    parent_config = None
    
    while last_config.parent != None:
        last_config.parent.child = last_config
        last_config = last_config.parent
        
    parent_config = last_config
    
    data = { 'pool' : pool, 'parent_config' : parent_config, 'instances' : instances }
    
    return render(request, 'pools/view.html', data)

@login_required(login_url='/login/')
def viewConfig(request, configid):
    config = get_object_or_404(PoolConfiguration, pk=configid)
    
    data = { 'config' : config }
    
    return render(request, 'pools/config.html', data)

@login_required(login_url='/login/')
def deletePool(request, poolid):
    pass

@login_required(login_url='/login/')
def deleteConfig(request, configid):
    pass