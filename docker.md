# Using ngenCERF-server Dockerfile

## Requirements

> [!CAUTION]
> GitLab is not supported by this repository. Anything connected to GitLab and NGWPC are non-functional

To build and run the ngenCERF-server container, you will need the following software installed and running on your system:
- Docker Engine
- Docker Compose 

You will also need files with the following credentials:
- AWS S3 credentials: saved to ./.aws_credentials
- NGWPC gitlab Personal Access Token (PAT): saved to ~/.gitlab_token.

This Docker container pulls images from the NOAA-OWP official Docker registry, so you will need to be logged into that registry. Using your gitlab credentials, login to the registry using the following command:
```
$ docker login registry.sh.nextgenwaterprediction.com
```

This will also create directories to persist data for:
1. the PostgreSQL database
1. initialization data for the ngencerf-server application. 
1. data from the ngen-cal tool

Your directory structure should look like this:
```
$ tree
.
├── data
│   ├── db
│   ├── ngen-cal-data
|   └── .ngencerf-init
└── ngencerf-server
```

So that ngenCERF-server can find the data written by ngen-cal, you should update the value of the variable NGEN_CAL_DATA_PATH to point the fully-qualified path of your data directory:
```
NGEN_CAL_DATA_PATH=/home/<username>/data/ngen-cal-data/
```

## Running ngenCERF-server

> [!CAUTION]
> This gitlab instance is unavailable for outside use

It is recommended to use the [ngencerf-docker](https://gitlab.sh.nextgenwaterprediction.com/NGWPC/nwm-ngen/ngencerf-docker/) project to run the full ngenCERF application stack at once. However, if you would like to just run the back-end services in isolation, execute the following command:
```
docker compose up
```

This will start instances of the following:
- ngencerf-server, running at the address http://localhost:8000
- PostgreSQL, running at the address localhost:5432

## Troubleshooting

### Forcing a container rebuild

If for some reason you don't see code updates being pulled into your ngencerf-server container run the following command to force a rebuild of the container:
```
docker compose up --build
```

After the rebuild is complete it will restart all the necessary services.

### Forcing static data loads

By default, the first time this container is run it will perform a load of all the necessary static data into the database. When complete it will write the file `../data/.ngencerf-init/.load_static`. You can delete this file to force the data to be reloaded the next time your start the application.

### Executing custom commands in a running container

If there is a need to connect to a container to issue commands from a terminal, perform the following steps:
1. Get a list of the running containers by executing the following command:
```
docker container ls
```
2. Attach a terminal to that container:
```
docker exec -it <container_id> bash
```
3. Execute any needed commands from that terminal.
4. Issue the following command to disconnect:
```
exit
```

## Future Improvements 

- Data mounts for ngen-cal-work directory
- Separate configuration to allow discrete production and development environments.

