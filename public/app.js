const orbs=document.querySelectorAll('.hero-art .orb');
if(!matchMedia('(prefers-reduced-motion: reduce)').matches&&orbs.length){
  let raf=null;
  const start=performance.now();
  const tick=now=>{
    const t=(now-start)/1000;
    orbs.forEach((el,i)=>{
      const d=6+i*3;
      el.style.transform=`translate3d(${Math.sin(t*.35+i*1.7)*d}px,${Math.cos(t*.28+i*2.1)*d}px,0)`;
    });
    raf=requestAnimationFrame(tick);
  };
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){cancelAnimationFrame(raf);raf=null}
    else if(!raf)raf=requestAnimationFrame(tick);
  });
  raf=requestAnimationFrame(tick);
}
document.querySelectorAll('details').forEach(d=>{
  d.addEventListener('toggle',()=>{
    if(d.open)document.querySelectorAll('details').forEach(o=>{if(o!==d)o.open=false});
  });
});
